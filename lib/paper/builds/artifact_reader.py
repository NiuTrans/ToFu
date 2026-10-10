"""Leased build readers for files and retained chunks.

The repository owns identity, admission and leases. These readers expose the
same seekable artifact port; a retained stream holds at most one decoded chunk.
HTTP and location consumers never require a materialized build directory.
"""
from __future__ import annotations

import io
from contextlib import ExitStack, contextmanager

from .compiler import artifact_metadata
from .contract import limit
from .errors import BuildError


def _metadata(receipt, name):
    artifact = next((row for row in receipt['artifacts'] if row['name'] == name), None)
    if artifact is None:
        raise BuildError('build_not_found', 'Paper artifact is unavailable')
    return artifact


class FileBuildArtifacts:
    def __init__(self, build, *, fallback=None):
        self.receipt = build.receipt
        self.directory = build.directory
        self.fallback = fallback

    @contextmanager
    def open_artifact(self, name):
        with ExitStack() as reads:
            try:
                metadata = _metadata(self.receipt, name)
                path = self.directory / name
                if artifact_metadata(path) != metadata:
                    raise BuildError('artifact_changed', 'Paper artifact failed integrity verification')
                artifact = reads.enter_context(path.open('rb')), metadata
            except (BuildError, OSError) as cache_error:
                if self.fallback is None:
                    raise
                try:
                    retained = self.fallback()
                except BuildError as missing:
                    if missing.code == 'build_not_found':
                        raise cache_error from missing
                    raise
                self.receipt = retained.receipt
                artifact = reads.enter_context(retained.open_artifact(name))
            yield artifact


class RetainedBuildArtifacts:
    def __init__(self, receipt, read_chunk):
        self.receipt = receipt
        self.read_chunk = read_chunk

    @contextmanager
    def open_artifact(self, name):
        metadata = _metadata(self.receipt, name)
        first = 0
        for artifact in self.receipt['artifacts']:
            if artifact['name'] == name:
                break
            first += (int(artifact['size_bytes']) + limit('retained_chunk_bytes') - 1) // limit('retained_chunk_bytes')
        with RetainedArtifactStream(int(metadata['size_bytes']), first, self.read_chunk) as stream:
            yield stream, metadata


class RetainedArtifactStream(io.RawIOBase):
    def __init__(self, size, first, read_chunk):
        super().__init__()
        self.size, self.first, self.read_chunk = size, first, read_chunk
        self.position = 0
        self.chunk_index = -1
        self.chunk = b''

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        self._checkClosed()
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END):
            raise ValueError('Invalid artifact seek origin')
        position = (0 if whence == io.SEEK_SET else self.position if whence == io.SEEK_CUR else self.size) + offset
        if position < 0:
            raise ValueError('Invalid artifact seek position')
        self.position = position
        return position

    def readinto(self, buffer):
        self._checkClosed()
        if not buffer or self.position >= self.size:
            return 0
        chunk_size = limit('retained_chunk_bytes')
        index, offset = divmod(self.position, chunk_size)
        if self.chunk_index != index:
            self.chunk = b''
            data = self.read_chunk(self.first + index)
            if len(data) != min(chunk_size, self.size - index * chunk_size):
                raise BuildError('artifact_changed', 'Retained paper chunk has an invalid length')
            self.chunk, self.chunk_index = data, index
        count = min(len(buffer), len(self.chunk) - offset)
        memoryview(buffer)[:count] = memoryview(self.chunk)[offset:offset + count]
        self.position += count
        return count

    def close(self):
        self.chunk = b''
        super().close()
