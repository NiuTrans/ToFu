"""Own temporary visual bytes until the knowledge store publishes or rolls back.

OwnedAssetSequence preserves list-style ordered reads while retaining only file
offsets in RAM. Metadata edits never duplicate original image bytes. No imports
of storage or configuration; the extraction/store caller owns close.
"""
import json
import tempfile
import threading
from collections.abc import Sequence


class AssetSpoolError(OSError):
    """A temporary visual artifact could not be retained or read faithfully."""


class OwnedAssetSequence(Sequence):
    def __init__(self):
        self._stream = None
        self._entries = []
        self._lock = threading.RLock()
        self._closed = False

    def __len__(self):
        return len(self._entries)

    def _write(self, value):
        if self._stream is None:
            self._stream = tempfile.TemporaryFile(mode='w+b', buffering=0)
        self._stream.seek(0, 2)
        offset = self._stream.tell()
        view = memoryview(value)
        while view:
            written = self._stream.write(view)
            if not written:
                raise OSError('short asset spool write')
            view = view[written:]
        return offset, len(value)

    def append(self, asset):
        metadata = {key: value for key, value in asset.items() if key != 'raw'}
        encoded = json.dumps(metadata, ensure_ascii=False).encode('utf-8')
        with self._lock:
            if self._closed:
                raise AssetSpoolError('asset spool is closed')
            try:
                raw_offset, raw_size = self._write(asset['raw'])
                meta_offset, meta_size = self._write(encoded)
            except OSError as error:
                raise AssetSpoolError('could not retain extracted visual evidence') from error
            self._entries.append((meta_offset, meta_size, raw_offset, raw_size))

    def _read(self, offset, size):
        self._stream.seek(offset)
        pieces = []
        remaining = size
        while remaining:
            value = self._stream.read(remaining)
            if not value:
                raise AssetSpoolError('asset spool ended before the retained record')
            pieces.append(value)
            remaining -= len(value)
        return b''.join(pieces)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        with self._lock:
            if self._closed:
                raise AssetSpoolError('asset spool is closed')
            meta_offset, meta_size, raw_offset, raw_size = self._entries[index]
            try:
                result = json.loads(self._read(meta_offset, meta_size))
                result['raw'] = self._read(raw_offset, raw_size)
                return result
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise AssetSpoolError('could not read retained visual evidence') from error

    def update_metadata(self, index, **values):
        if 'raw' in values:
            raise ValueError('original visual bytes are immutable')
        with self._lock:
            if self._closed:
                raise AssetSpoolError('asset spool is closed')
            meta_offset, meta_size, raw_offset, raw_size = self._entries[index]
            metadata = json.loads(self._read(meta_offset, meta_size))
            metadata.update(values)
            try:
                meta_offset, meta_size = self._write(
                    json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
            except OSError as error:
                raise AssetSpoolError('could not update retained visual metadata') from error
            self._entries[index] = (meta_offset, meta_size, raw_offset, raw_size)

    def close(self):
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._closed = True

    def __del__(self):
        try:
            self.close()
        except OSError:
            # Explicit extraction/store close owns cleanup errors; this only
            # protects an abandoned parser result during interpreter teardown.
            pass


def close_extracted_assets(parsed):
    assets = parsed.get('assets')
    if isinstance(assets, OwnedAssetSequence):
        assets.close()
