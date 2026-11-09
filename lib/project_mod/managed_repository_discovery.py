"""Pruned discovery walk for managed repository preparation, resumable through a journal.

discover() lists a plain folder's directories top-down and never enters what
preparation can classify without reading its content: nested Git repositories,
virtual environments and nested projects (a directory holding its own build or
package manifest, ``PROJECT_MANIFESTS``), nor any directory the caller skips
(runtime trees and paths the folder's ignore files hide). A folder of a million
files that consists of copied projects and package caches is therefore read in
the directory listings above those boundaries, never file by file.

Each listed directory is appended to a ``Journal`` kept outside the folder. An
interrupted discovery resumes from the listings it recorded instead of starting
again; the caller deletes the journal once preparation succeeds. The journal is
reconstructible: a different header (rules fingerprint, root) or a torn record
discards it from that point.

Entry points: discover, Journal, PROJECT_MANIFESTS. Standard library only: the
caller (managed_repository) supplies the skip predicate, which owns the runtime
and ignore rules, and the progress hook.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

# Files that make a directory the root of a project, as each ecosystem defines
# its build or package manifest. Dependency lists (requirements.txt) and generic
# build scripts (Makefile) do not: plain note and data folders hold them too.
PROJECT_MANIFESTS = frozenset({
    'pyproject.toml', 'setup.py', 'package.json', 'deno.json', 'deno.jsonc', 'Cargo.toml',
    'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'build.sbt', 'composer.json',
    'Gemfile', 'mix.exs', 'pubspec.yaml', 'Package.swift', 'CMakeLists.txt', 'meson.build',
    'MODULE.bazel', 'cabal.project', 'stack.yaml',
})
_JOURNAL_FORMAT = 1
_KINDS = frozenset({'directory', 'repository', 'environment', 'project', 'unlisted'})


@dataclass
class Discovery:
    """What the walk found; paths are folder-relative with '/' separators."""
    declared: bool = False  # the folder root holds a project manifest
    units: dict[str, str] = field(default_factory=dict)  # repositories and environments
    projects: list[str] = field(default_factory=list)  # outermost nested projects
    # Directories made only of nested projects (outermost first): path -> projects inside.
    project_folders: dict[str, int] = field(default_factory=dict)
    listed: int = 0
    resumed: int = 0

    def project_shares(self) -> dict[str, str]:
        """Nested projects as few shared paths: whole project folders, then the rest."""
        shares = {path: f'{count:,} nested projects' for path, count in self.project_folders.items()}
        for path in self.projects:
            if not _ancestors_in(path, shares):
                shares[path] = 'nested project'
        return shares


def _ancestors_in(path: str, paths) -> list[str]:
    parts = path.split('/')
    return [ancestor for ancestor in ('/'.join(parts[:index]) for index in range(1, len(parts)))
            if ancestor in paths]


class Journal:
    """Append-only listings of one discovery, outside the folder; ``path=None`` keeps none."""

    def __init__(self, path: Path | None, *, root: str, rules: str):
        self._path = path
        self._header = {'journal': _JOURNAL_FORMAT, 'root': root, 'rules': rules}
        self._stream = None

    def load(self) -> dict[str, dict]:
        """Recorded listings; a mismatched header discards all, a torn record its tail."""
        if self._path is None:
            return {}
        try:
            data = self._path.read_bytes()
        except OSError:
            return {}
        records, offset, header = {}, 0, None
        for line in data.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                break
            try:
                value = json.loads(line)
            except ValueError:
                break
            if header is None:
                header = value
                if header != self._header:
                    break
            elif _valid(value):
                records[value['d']] = value
            else:
                break
            offset += len(line)
        if header != self._header:
            self.discard()
            return {}
        if offset != len(data):
            os.truncate(self._path, offset)
        return records

    def append(self, record: dict) -> None:
        if self._path is None:
            return
        if self._stream is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = open(self._path, 'ab')
            if self._stream.tell() == 0:
                self._write(self._header)
        self._write(record)

    def _write(self, value: dict) -> None:
        # ensure_ascii keeps undecodable (surrogate-escaped) names representable.
        self._stream.write(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode() + b'\n')
        self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def discard(self) -> None:
        self.close()
        if self._path is not None:
            self._path.unlink(missing_ok=True)


def _valid(record) -> bool:
    if not isinstance(record, dict) or record.get('k') not in _KINDS:
        return False
    directory = record.get('d')
    if not isinstance(directory, str) or (directory and not _relative(directory)):
        return False
    children = record.get('c', [])
    prefix = directory + '/' if directory else ''
    return isinstance(children, list) and all(
        isinstance(child, str) and child.startswith(prefix) and _relative(child)
        and '/' not in child[len(prefix):] for child in children)


def _relative(path: str) -> bool:
    return (not path.startswith('/') and '\0' not in path
            and all(part not in ('', '.', '..') for part in path.split('/')))


def _list(root: Path, directory: str, skip) -> dict:
    """One listing: a boundary kind, or the child directories to enter next."""
    try:
        with os.scandir(root / directory if directory else root) as entries:
            listing = [(entry.name, entry.is_dir(follow_symlinks=False),
                        entry.is_file(follow_symlinks=False)) for entry in entries]
    except OSError:
        # Gone or unreadable: Git's own walk meets it again and decides.
        return {'d': directory, 'k': 'unlisted'}
    directories = {name for name, is_directory, _ in listing if is_directory}
    files = {name for name, _, is_file in listing if is_file}
    if directory:
        if '.git' in directories or '.git' in files:
            return {'d': directory, 'k': 'repository'}
        if 'pyvenv.cfg' in files or 'conda-meta' in directories:
            return {'d': directory, 'k': 'environment'}
        if files & PROJECT_MANIFESTS:
            return {'d': directory, 'k': 'project'}
    children, others = [], len(listing) > len(directories)
    for name in sorted(directories):
        child = f'{directory}/{name}' if directory else name
        if skip(child):
            others = True
        else:
            children.append(child)
    record = {'d': directory, 'k': 'directory', 'c': children, 'x': others}
    if not directory:
        record['m'] = bool(files & PROJECT_MANIFESTS)
    return record


def discover(root: Path, journal: Journal, *, skip, advanced) -> Discovery:
    """Walk ``root`` down to its boundaries; ``advanced()`` runs after each directory.

    ``skip(path)`` excludes a child directory and everything below it.
    Directories recorded in ``journal`` are not listed again.
    """
    records = journal.load()
    found = Discovery()
    order, stack = [], ['']
    try:
        while stack:
            directory = stack.pop()
            record = records.get(directory)
            if record is None:
                record = _list(root, directory, skip)
                journal.append(record)
                records[directory] = record
                found.listed += 1
            else:
                found.resumed += 1
            order.append(directory)
            stack.extend(reversed(record.get('c', [])))
            advanced()
    finally:
        journal.close()
    reasons = {'repository': 'nested repository', 'environment': 'virtual environment'}
    projects: dict[str, int] = {}
    for directory in order:
        kind = records[directory]['k']
        if kind in reasons:
            found.units[directory] = reasons[kind]
        elif kind == 'project':
            projects[directory] = 1
    found.declared = bool(records[''].get('m'))
    found.projects = sorted(projects)
    # Deepest first: a directory holding nothing but nested projects is one folder of them.
    for directory in sorted((path for path in order if path and records[path]['k'] == 'directory'),
                            key=lambda path: path.count('/'), reverse=True):
        children = records[directory]['c']
        if not records[directory]['x'] and children and all(child in projects for child in children):
            projects[directory] = sum(projects[child] for child in children)
            found.project_folders[directory] = projects[directory]
    found.project_folders = {
        path: count for path, count in sorted(found.project_folders.items())
        if not _ancestors_in(path, found.project_folders)}
    return found
