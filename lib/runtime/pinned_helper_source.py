"""Spawn helper scripts from the source their caller loaded, never the live checkout.

Responsibility: a long-running process keeps caller code in memory while the
checkout beneath it changes. A helper spawned by path from the checkout then
runs newer or older code than the caller that built its arguments (2026-10-26:
after 49053ced2 changed git_index_snapshot_exec.py's argv, every hardened
``git diff`` of the running server crashed until a restart).

Entry point: PinnedHelperSource. The module that builds a helper's arguments
creates one at import and passes ``path()`` to the child. The source bytes are
read once, at that import. ``path()`` returns an immutable file holding exactly
those bytes, named by their SHA-256 inside one directory private to the user
under the system temporary directory (mode 0o700, ownership checked on every
call). Nothing belongs to a process: a re-exec, crash or SIGKILL leaves no
state behind, concurrent processes share identical bytes, and a file removed
by a temporary-file cleaner is rewritten from memory on the next call.
Growth follows helper source revisions, not process starts.

Only self-contained helpers qualify: standard library or installed packages.
A module that a helper imports from the checkout still loads from disk at
spawn time, so such workers keep a backward-compatible protocol instead.
Dependencies: standard library only.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile


def _private_directory() -> Path:
    owner = os.getuid() if hasattr(os, 'getuid') else None
    directory = Path(tempfile.gettempdir()) / (
        'tofu-pinned-helpers' if owner is None else f'tofu-pinned-helpers-{owner}')
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    status = os.lstat(directory)
    # Another user can pre-create this predictable name in a shared /tmp.
    if not stat.S_ISDIR(status.st_mode) or (owner is not None and (
            status.st_uid != owner or stat.S_IMODE(status.st_mode) & 0o077)):
        raise OSError(f'Pinned helper directory {directory} is not private to this user')
    return directory


class PinnedHelperSource:
    """One self-contained helper script, as read when its caller was imported."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        path = Path(path)
        self.name = path.name
        self._unavailable = ''
        try:
            self._source: bytes | None = path.read_bytes()
        except OSError as error:
            # Frozen builds ship no helper sources; only a spawn needs them.
            self._source = None
            self._unavailable = f'{type(error).__name__}: {error}'
            self._digest = ''
        else:
            self._digest = hashlib.sha256(self._source).hexdigest()[:24]

    def path(self) -> str:
        """Return a private file with the import-time source; OSError if unavailable."""
        if self._source is None:
            raise OSError(f'Helper source {self.name} was unavailable when its '
                          f'caller was imported ({self._unavailable})')
        version = _private_directory() / self._digest
        target = version / self.name
        if not target.is_file():
            version.mkdir(mode=0o700, exist_ok=True)
            descriptor, staged = tempfile.mkstemp(dir=version, prefix='.staging-')
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(self._source)
                # Readers never observe a partially written helper.
                os.replace(staged, target)
            except BaseException:
                Path(staged).unlink(missing_ok=True)
                raise
        return str(target)
