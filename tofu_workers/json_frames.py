"""Shared immutable JSON files and physical worker ownership.

JsonFrameStore encodes publications once on demand workers. Ordered transport
references pin anonymous file segments until delivery/withdrawal. File rotation
and idle worker reuse are caller-injected hints, never admission limits. The
module depends only on the standard library and tofu_workers.executor.
"""
from __future__ import annotations

import json
import io
import os
import copy
import tempfile
import threading
import weakref
from dataclasses import dataclass
from concurrent.futures import Future

from .executor import DemandThreadPoolExecutor

class _FrameFile:
    def __init__(self):
        self.file = tempfile.TemporaryFile(mode='w+b', buffering=0)
        self.lock = threading.Lock()
        self.byte_count = 0


@dataclass(frozen=True)
class StoredJsonFrame:
    owner: _FrameFile
    offset: int
    length: int

    def _read_bytes(self) -> bytes:
        # Only a worker touches file cursors. Holding this frame pins the file
        # across the complete read, even if all sockets disconnect meanwhile.
        def read_all(read):
            with io.BytesIO() as output:
                remaining = self.length
                while remaining:
                    chunk = read(min(remaining, 64 * 1024))
                    if not chunk:
                        raise OSError('JSON frame file ended before its owned range')
                    output.write(chunk)
                    remaining -= len(chunk)
                return output.getvalue()
        if hasattr(os, 'pread'):
            position = self.offset
            def read(size):
                nonlocal position
                chunk = os.pread(self.owner.file.fileno(), size, position)
                position += len(chunk)
                return chunk
            return read_all(read)
        with self.owner.lock:  # portable independent-reader fallback
            self.owner.file.seek(self.offset)
            return read_all(self.owner.file.read)

    def decode(self) -> dict:
        raw = self._read_bytes()
        return json.loads(raw)

    def text(self) -> str:
        return self._read_bytes().decode('utf-8')


class JsonFrameStore:
    def __init__(self, *, segment_bytes: int, reuse_workers: int,
                 thread_name_prefix: str = 'tofu-json-frame'):
        self._segment_bytes = max(1, int(segment_bytes))
        self._reuse_workers = max(1, int(reuse_workers))
        self._thread_name_prefix = thread_name_prefix
        self._close_thread = None
        self._lifecycle = threading.Lock()
        self._append_lock = threading.Lock()
        self._executor = None
        self._closed = False
        self._current = None
        self._files = weakref.WeakSet()

    def _submit(self, fn, *args) -> Future:
        with self._lifecycle:
            if self._closed:
                raise RuntimeError('JSON frame owner is closing')
            if self._executor is None:
                self._executor = DemandThreadPoolExecutor(
                    max_workers=self._reuse_workers,
                    thread_name_prefix=self._thread_name_prefix)
            executor = self._executor
        return executor.submit(fn, *args)

    def submit_transport(self, function, *args) -> Future:
        """Own associated blocking transport I/O under the same joined close."""
        return self._submit(function, *args)

    def prepare(self, frame: dict) -> Future:
        try:
            return self._submit(self._encode, copy.deepcopy(frame))
        except Exception as error:
            # Preparation failure is one transport outcome. Raising into the
            # bus publisher would invoke its local fallback and duplicate
            # delivery observers for the same publication.
            failed = Future()
            failed.set_exception(error)
            return failed

    def read(self, frame: StoredJsonFrame, *, encoded: bool = False) -> Future:
        return self._submit(frame.text if encoded else frame.decode)

    def _encode(self, frame: dict) -> StoredJsonFrame:
        # Standard-library output avoids retaining orjson's oversized backing
        # allocations while concurrent writers own large encoded payloads.
        raw = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        with self._append_lock:
            owner = self._current() if self._current is not None else None
            if owner is None or owner.byte_count + len(raw) > self._segment_bytes:
                owner = _FrameFile()
                self._current = weakref.ref(owner)
                with self._lifecycle:
                    self._files.add(owner)
            offset = owner.byte_count
            owner.byte_count += len(raw)
        # Range reservation is short and independent of actual disk I/O. One
        # blocked write/read cannot hold the arena allocator or another range.
        pending = memoryview(raw)
        if hasattr(os, 'pwrite'):
            position = offset
            while pending:
                count = os.pwrite(owner.file.fileno(), pending, position)
                if not count:
                    raise OSError('JSON frame write made no progress')
                pending = pending[count:]
                position += count
        else:
            with owner.lock:
                owner.file.seek(offset)
                while pending:
                    count = owner.file.write(pending)
                    if not count:
                        raise OSError('JSON frame write made no progress')
                    pending = pending[count:]
        # Failed writes do not publish a range. Prior and concurrent complete
        # ranges remain valid; the gap retires with this anonymous segment.
        return StoredJsonFrame(owner, offset, len(raw))

    def snapshot(self) -> dict:
        with self._lifecycle:
            files = tuple(self._files)
        return {'event_spool_files': len(files),
                'event_spool_bytes': sum(file.byte_count for file in files)}

    def close(self, *, wait: bool = True) -> None:
        with self._lifecycle:
            self._closed = True
            executor = self._executor
            joining = self._close_thread
        if executor is not None:
            executor.shutdown(wait=wait and joining is None, cancel_futures=True)
        if wait and joining is not None and joining is not threading.current_thread():
            joining.join()

    def __del__(self):
        # Explicit hub/socket close is the joined ownership boundary. A legacy
        # standalone client dropped without close must still retire its idle
        # carriers; do not block finalization or join the current worker here.
        if hasattr(self, '_lifecycle'):
            try:
                self.close(wait=False)
            except Exception:
                pass

    @property
    def is_closed(self) -> bool:
        with self._lifecycle:
            return self._closed

    def close_with_timeout(self, timeout: float) -> bool:
        """Bound observation while retaining the exact physical join carrier.

        A false result retains the close thread for subsequent observation;
        it never means that running encoders or their files have been released.
        """
        with self._lifecycle:
            self._closed = True
            if self._executor is None:
                return True
            if self._close_thread is None:
                joining = threading.Thread(
                    target=self._executor.shutdown,
                    kwargs={'wait': True, 'cancel_futures': True},
                    name=self._thread_name_prefix + '-close', daemon=False)
                self._close_thread = joining
                try:
                    joining.start()
                except BaseException:
                    if not joining.is_alive():
                        self._close_thread = None
                    raise
            joining = self._close_thread
        if joining is not threading.current_thread():
            joining.join(max(0.0, timeout))
        return not joining.is_alive()
