"""Directory durability for an owner-scoped knowledge file publication.

Entry points: prepare_directory and sync_publication_directories. The caller
supplies an existing durable anchor (application data root, or test fixture
root); only descendants are created. File contents are fsynced by the writer.
POSIX directory entries are flushed once per publication before metadata entry.
Platforms without O_DIRECTORY keep their existing file-fsync/rename behavior.
No global cache, worker, lock or payload retention is introduced.
"""
import os
from pathlib import Path


def _chain(directory: Path, anchor: Path) -> list[Path]:
    directory, anchor = Path(os.path.abspath(directory)), Path(os.path.abspath(anchor))
    if directory == anchor or not directory.is_relative_to(anchor):
        raise ValueError('Knowledge file directory must descend from its durable anchor')
    result = [directory]
    while result[-1] != anchor:
        result.append(result[-1].parent)
    return result


def prepare_directory(directory: Path, *, anchor: Path) -> None:
    _chain(directory, anchor)
    if not anchor.is_dir():
        raise FileNotFoundError('Knowledge file durability anchor is missing: ' + str(anchor))
    directory.mkdir(parents=True, exist_ok=True)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    except BaseException as failure:
        try:
            os.close(descriptor)
        except BaseException as cleanup:
            failure.add_note('Directory descriptor close also failed: ' + type(cleanup).__name__)
        raise
    else:
        os.close(descriptor)


def sync_publication_directories(roots: list[tuple[Path, Path]]) -> None:
    if not hasattr(os, 'O_DIRECTORY'):
        return
    directories = {path for directory, anchor in roots for path in _chain(directory, anchor)}
    # Child contents precede their parent links; shared owner/base/anchor paths
    # are flushed once even when a document publishes thousands of visuals.
    for directory in sorted(directories, key=lambda path: (-len(path.parts), str(path))):
        _sync_directory(directory)
