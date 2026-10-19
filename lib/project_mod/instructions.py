"""Applicable project guidance acquisition, scoped to declared execution roots.

read_root_guidance loads root instruction files. read_scoped_guidance adds only
nested AGENTS.md on a target's directory chain, never siblings or server/cwd
ancestors. Acquisition is complete-or-error and read-only; guidance cannot grant
filesystem access. Callers own delivery and task lifetime, not this module.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import os
import stat

ROOT_RULE_FILES = ('CLAUDE.md', '.cursorrules', 'AGENTS.md', 'COPILOT.md')
MAX_GUIDANCE_BYTES = 1024 * 1024
MAX_GUIDANCE_FILES = 128


def _read_rule(path: Path, root: Path, *, remaining_bytes=MAX_GUIDANCE_BYTES,
               remaining_files=MAX_GUIDANCE_FILES) -> dict | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f'Project guidance is not a regular file: {path}')
    # A rule symlink may not import guidance from a different project.
    if not path.resolve().is_relative_to(root):
        raise PermissionError(f'Project guidance escapes its declared root: {path}')
    if remaining_files <= 0 or info.st_size > remaining_bytes:
        raise ValueError(f'Project guidance exceeds the remaining acquisition bound: {path}')
    # Bind the read to no-follow directory descriptors, so swapping either
    # the rule or a parent directory after validation cannot import foreign bytes.
    if os.name == 'posix':
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.relative_to(root).parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            source_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        with os.fdopen(source_fd, 'rb') as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(f'Opened project guidance is not a regular file: {path}')
            if opened.st_size > remaining_bytes:
                raise ValueError(f'Project guidance exceeds the remaining acquisition bound: {path}')
            raw = source.read(remaining_bytes + 1)
    else:
        # Reparse points are rejected on Windows; runtime file guards retain
        # platform-specific write containment independently of this read port.
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise PermissionError(f'Project guidance is a reparse point: {path}')
        with path.open('rb') as source:
            raw = source.read(remaining_bytes + 1)
    if len(raw) > remaining_bytes:
        raise ValueError(f'Project guidance exceeds the acquisition bound: {path}')
    text = raw.decode('utf-8', errors='replace')
    return {'path': str(path), 'root': str(root), 'scope': str(path.parent), 'content': text,
            'hash': sha256(raw).hexdigest()}


def _append_rule(rows: list[dict], path: Path, root: Path) -> None:
    remaining = MAX_GUIDANCE_BYTES - sum(len(row['content'].encode('utf-8')) for row in rows)
    row = _read_rule(path, root, remaining_bytes=remaining,
                     remaining_files=MAX_GUIDANCE_FILES - len(rows))
    if row is not None:
        rows.append(row)
        _check_bounds(rows)


def read_root_guidance(root: str) -> list[dict]:
    root_path = Path(root).expanduser().resolve()
    rows = []
    for name in ROOT_RULE_FILES:
        _append_rule(rows, root_path / name, root_path)
    _check_bounds(rows)
    return rows


def read_scoped_guidance(root: str, target: str) -> list[dict]:
    root_path = Path(root).expanduser().resolve()
    selected = Path(target).expanduser().resolve()
    if not selected.is_relative_to(root_path):
        raise PermissionError('Instruction target is outside its declared root')
    rows = read_root_guidance(str(root_path))
    directory = selected if selected.is_dir() else selected.parent
    chain = [directory, *directory.parents]
    for ancestor in reversed(chain[:chain.index(root_path)] if root_path in chain else []):
        _append_rule(rows, ancestor / 'AGENTS.md', root_path)
    _check_bounds(rows)
    return rows


def _check_bounds(rows: list[dict]) -> None:
    if (len(rows) > MAX_GUIDANCE_FILES or
            sum(len(row['content'].encode('utf-8')) for row in rows) > MAX_GUIDANCE_BYTES):
        raise ValueError('Applicable project guidance exceeds the acquisition bound; nothing was omitted')


def render_guidance(rows: list[dict]) -> str:
    _check_bounds(rows)
    sections = []
    for row in rows:
        if not row['content'].strip():
            continue
        sections.append(
            f"\nProject Intelligence — {Path(row['path']).name}\n"
            f"Source: {row['path']}\nApplies only within: {row['scope']}\n"
            "Nested AGENTS.md specializes its subtree; guidance does not grant access.\n\n"
            + row['content'].strip())
    return '\n'.join(sections)
