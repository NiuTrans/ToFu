"""Quart request bytes with a RAM hint and owned temporary-file overflow.

The existing ASGI receiver/handler own this body until both are joined. Small
bodies stay in memory; overflow uses one anonymous local file. Multipart reads
consume fixed transport chunks, while explicit whole-body reads materialize the
actual body. The RAM hint never refuses a request. Dependencies: Quart, stdlib.
"""
from __future__ import annotations

import asyncio
import functools
import tempfile
import threading

from quart.wrappers.request import Body, Request
from werkzeug.exceptions import RequestEntityTooLarge


READ_CHUNK_BYTES = 64 * 1024


def memory_hint_bytes(snapshot) -> int:
    capacity = snapshot.effective_memory_capacity_mb
    available = snapshot.effective_memory_available_mb
    if capacity is None or available is None:
        return READ_CHUNK_BYTES
    return max(0, min(capacity * 1024 * 1024 // 512,
                      available * 1024 * 1024 // 256))


async def _owned_io(function, *args):
    """Join an accepted file operation before cancellation releases its owner."""
    task = asyncio.get_running_loop().run_in_executor(None, functools.partial(function, *args))
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            cancelled = error
            if task.done():
                if not task.cancelled():
                    task.exception()
                break
        except BaseException:
            if cancelled is not None:
                raise cancelled
            raise
    if cancelled is not None:
        raise cancelled
    return result


class _BodyBuffer:
    def __init__(self, memory_hint: int):
        self.memory_hint = max(0, memory_hint)
        self.memory = bytearray()
        self.file = None
        self.read_offset = 0
        self.pending = 0
        self.lock = threading.Lock()

    def __len__(self):
        return self.pending

    def _extend_memory_locked(self, data):
        if self.file is not None or self.pending + len(data) > self.memory_hint:
            return False
        try:
            self.memory.extend(data)
            self.pending += len(data)
            return True
        except MemoryError:
            del self.memory[self.pending:]
            return False

    def try_memory(self, data):
        with self.lock:
            return self._extend_memory_locked(data)

    def extend(self, data: bytes):
        with self.lock:
            if self._extend_memory_locked(data):
                return
            # Failure of the RAM preference never rejects the source bytes.
            if self.file is None:
                self.file = tempfile.TemporaryFile(mode='w+b')
                self.file.write(self.memory)
                self.memory.clear()
            self.file.seek(0, 2)
            self.file.write(data)
            self.pending += len(data)

    def read_chunk(self):
        with self.lock:
            size = min(self.pending, READ_CHUNK_BYTES)
            if self.file is None:
                data = bytes(self.memory[:size])
                del self.memory[:size]
            else:
                self.file.seek(self.read_offset)
                data = self.file.read(size)
                if len(data) != size:
                    raise OSError('request body spool ended before its owned bytes')
                self.read_offset += size
            self.pending -= size
            if self.file is not None and self.pending == 0:
                self.file.seek(0)
                self.file.truncate(0)
                self.read_offset = 0
            return data

    def materialize(self):
        with self.lock:
            if self.file is None:
                return bytes(self.memory)
            self.file.seek(self.read_offset)
            data = self.file.read(self.pending)
            if len(data) != self.pending:
                raise OSError('request body spool ended before its owned bytes')
            return data

    def clear(self):
        with self.lock:
            self.memory.clear()
            if self.file is not None:
                self.file.close()
                self.file = None
            self.read_offset = self.pending = 0


class DemandRequestBody(Body):
    def __init__(self, expected_content_length, max_content_length, *, memory_hint=None):
        super().__init__(expected_content_length, max_content_length)
        if memory_hint is None:
            memory_hint = READ_CHUNK_BYTES
        self._data = _BodyBuffer(memory_hint)
        self.transport_owned = False
        self.closed = False
        self._received_bytes = 0
        self._io_inflight = 0

    def configure_memory_hint(self, hint):
        if self._received_bytes:
            raise RuntimeError("request body memory hint must precede receiving")
        self._data.memory_hint = max(0,int(hint))

    def _admit_chunk(self, data):
        if not data or self._must_raise is not None:
            return False
        if self.closed:
            raise RuntimeError('request body owner is closed')
        self._received_bytes += len(data)
        if self._max_content_length is not None and self._received_bytes > self._max_content_length:
            self._must_raise = RequestEntityTooLarge()
            self.set_complete()
            return False
        return True

    def append(self, data):
        if not self._admit_chunk(data):
            return
        try:
            self._data.extend(data)
        except OSError as error:
            self._must_raise = error
            self.set_complete()
            return
        self._has_data.set()

    async def append_async(self, data):
        if not self._admit_chunk(data):
            return
        try:
            if self._data.file is None and not self._io_inflight and self._data.try_memory(data):
                pass
            else:
                self._io_inflight += 1
                try:
                    await _owned_io(self._data.extend, data)
                finally:
                    self._io_inflight -= 1
        except OSError as error:
            self._must_raise = error
            self.set_complete()
            return
        self._has_data.set()

    async def __anext__(self):
        if self._must_raise is not None:
            raise self._must_raise
        if not self._complete.is_set():
            await self._has_data.wait()
        if self._must_raise is not None:
            raise self._must_raise
        if self._complete.is_set() and not len(self._data):
            raise StopAsyncIteration
        if self._data.file is not None or self._io_inflight:
            data = await _owned_io(self._data.read_chunk)
        else:
            data = self._data.read_chunk()
        if not len(self._data):
            self._has_data.clear()
        return data

    def __await__(self):
        async def materialize():
            if self._must_raise is not None:
                raise self._must_raise
            await self._complete.wait()
            if self._must_raise is not None:
                raise self._must_raise
            if self._data.file is not None:
                return await _owned_io(self._data.materialize)
            return self._data.materialize()
        return materialize().__await__()

    async def clear_async(self):
        if self._data.file is not None or self._io_inflight:
            await _owned_io(self._data.clear)
        else:
            self._data.clear()

    async def close_async(self):
        self.closed = True
        await self.clear_async()

    def close(self):
        self._data.clear()
        self.closed = True


class DemandRequest(Request):
    body_class = DemandRequestBody

    async def get_data(self, cache=True, as_text=False, parse_form_data=False):
        data = await super().get_data(cache=True, as_text=as_text, parse_form_data=parse_form_data)
        if not cache:
            await self.body.clear_async()
        return data

    def make_form_data_parser(self):
        parser = super().make_form_data_parser()
        original_factory = parser.stream_factory
        def owned_stream(*args, **kwargs):
            stream = original_factory(*args, **kwargs)
            self.__dict__.setdefault('_owned_form_streams', []).append(stream)
            return stream
        parser.stream_factory = owned_stream
        return parser

    async def close(self):
        errors = []
        try:
            await super().close()
        except BaseException as error:
            errors.append(error)
        for stream in self.__dict__.get('_owned_form_streams', ()):
            try:
                stream.close()
            except BaseException as error:
                errors.append(error)
        if not self.body.transport_owned:
            try:
                await self.body.close_async()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise errors[0]
