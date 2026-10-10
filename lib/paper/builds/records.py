"""Freeze application-owned source records for the shared paper compiler.

SourceRecords validates a complete byte inventory before queue admission and
writes it only inside an exclusively owned build stage. Unlike filesystem
discovery, explicit records include every supplied file, including metadata.
Dependencies: source manifest contract, cancellation and immutable snapshots.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .contract import limit
from .errors import BuildError, check_cancelled
from .sources import SourceSnapshot, manifest_digest


@dataclass(frozen=True)
class SourceRecords:
    files: tuple[tuple[str, bytes], ...]
    main_file: str

    @classmethod
    def from_text(cls, files: list[dict], *, main_file: str = 'main.tex') -> 'SourceRecords':
        if not files or len(files) > limit('source_files'):
            raise BuildError('source_limit', 'Choose a bounded, nonempty source tree')
        records = []
        names = set()
        size = 0
        for row in files:
            if not isinstance(row, dict):
                raise BuildError('source_path', 'Source records must be objects')
            path, content = row.get('path'), row.get('content')
            if not isinstance(path, str) or not isinstance(content, str):
                raise BuildError('source_path', 'Source records require exact text and relative paths')
            relative = PurePosixPath(path)
            if (not relative.parts or len(path) > 240 or relative.is_absolute() or ':' in path
                    or '\\' in path or '\x00' in path or '..' in relative.parts
                    or relative.as_posix() != path or path.casefold() in names
                    or any(PureWindowsPath(part).is_reserved() for part in relative.parts)):
                raise BuildError('source_path', 'Source paths must be distinct portable relative files')
            names.add(path.casefold())
            if len(content) > limit('source_file_bytes'):
                raise BuildError('source_limit', 'Source record exceeds the compiler byte budget')
            data = content.encode('utf-8')
            size += len(data)
            if len(data) > limit('source_file_bytes') or size > limit('source_bytes'):
                raise BuildError('source_limit', 'Source records exceed the compiler byte budget')
            records.append((path, data))
        if main_file not in {path for path, _ in records}:
            raise BuildError('main_file', 'Choose a main file from the exact source tree')
        for path, _ in records:
            if any(parent.as_posix().casefold() in names for parent in PurePosixPath(path).parents):
                raise BuildError('source_path', 'A source file cannot also be a directory')
        return cls(tuple(sorted(records)), main_file)

    @property
    def manifest(self) -> list[dict]:
        return [{'path': path, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for path, data in self.files]

    def freeze(self, destination: Path, *, cancel=None) -> SourceSnapshot:
        check_cancelled(cancel)
        destination.mkdir(parents=True, exist_ok=False)
        for path, data in self.files:
            check_cancelled(cancel)
            output = destination / path
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('xb') as stream:
                stream.write(data)
            output.chmod(0o444)
        rows = self.manifest
        return SourceSnapshot(destination, self.main_file, manifest_digest(rows),
                              tuple(rows), sum(row['size_bytes'] for row in rows))
