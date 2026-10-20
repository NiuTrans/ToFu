"""Request-bound concurrent tool callbacks on the native client's one reader.

Entry: NativeToolControl owns only active callbacks, their Stop signals and the
first callback failure. The existing client retains transport, authority checks,
terminal persistence and process teardown; tool handlers retain execution.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class _Callback:
    stopped: asyncio.Event
    work: asyncio.Task


class NativeToolControl:
    def __init__(self, client, process, configuration, handlers, approvals):
        self.client, self.process = client, process
        self.configuration, self.handlers, self.approvals = configuration, handlers, approvals
        self.pending: dict[str, _Callback] = {}
        self.failure: Exception | None = None

    def submit(self, value):
        from lib.agent_core.native_process import NativeProcessError
        identity = value['request_id']
        if not identity or identity in self.pending:
            raise NativeProcessError('native tool request repeated while active')
        if self.approvals is not None:
            self.approvals.consume(value)
        stopped = asyncio.Event()
        work = asyncio.create_task(self._invoke(value, stopped))
        self.pending[identity] = _Callback(stopped, work)

    async def _invoke(self, value, stopped):
        from lib.agent_core.native_process import ToolRequest, validate_model_content, _joined
        error = None
        content: Any = ''
        callback = None
        try:
            callback = asyncio.create_task(self.handlers[value['name']](ToolRequest(
                self.client.principal, self.configuration['task_id'], value['request_id'],
                value['name'], value['arguments'], value.get('call_id'), value.get('caller'),
                stopped, value.get('model_round'), value.get('model_call_id'))))
            self.client._tool_callbacks.add(callback)
            content = await asyncio.shield(callback)
        except asyncio.CancelledError:
            stopped.set()
            # A callback's own cancellation is an explicit failed result. The
            # group never cancels this task to detach the underlying handler.
            try:
                if callback is not None:
                    content = await _joined(callback)
                else:
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                error = RuntimeError('native tool callback cancelled before returning a result')
        except Exception as failure:
            error = failure
        finally:
            if callback is not None:
                try:
                    if not callback.done():
                        stopped.set()
                        await _joined(callback)
                finally:
                    self.client._tool_callbacks.discard(callback)
        if not isinstance(content, str):
            try:
                if self.configuration.get('tool_feedback', 'text') != 'multimodal':
                    raise TypeError('native tool callback must return text')
                validate_model_content(content)
            except (TypeError, ValueError) as failure:
                error = failure
        if error is not None:
            content = ''
            if self.failure is None:
                self.failure = error
        reply = self.client._encode({
            'version': self.client.protocol_version, 'type': 'tool_result',
            'request_id': value['request_id'], 'content': content,
            **({'failed': True} if error is not None else {}),
        }, min(self.client.budget.frame_bytes, self.configuration['tool_frame_bytes']))
        async with asyncio.timeout(self.client.budget.shutdown_seconds):
            # StreamWriter.write is synchronous: each complete encoded frame is
            # queued atomically on this event loop before any drain can yield.
            self.process.stdin.write(reply)
            await self.process.stdin.drain()

    def _reap(self):
        for identity, callback in tuple(self.pending.items()):
            if callback.work.done():
                del self.pending[identity]
                callback.work.result()

    async def readline(self):
        """One transport reader also observes callback transport failures."""
        frame = asyncio.create_task(self.process.stdout.readline())
        try:
            while True:
                self._reap()
                done, _ = await asyncio.wait({frame, *(item.work for item in self.pending.values())},
                    return_when=asyncio.FIRST_COMPLETED)
                self._reap()
                if frame in done:
                    return frame.result()
        finally:
            if not frame.done():
                frame.cancel()
            await asyncio.gather(frame, return_exceptions=True)

    async def settle(self):
        from lib.agent_core.native_process import _joined
        if self.pending:
            await _joined(asyncio.gather(*(item.work for item in self.pending.values()), return_exceptions=True))
        self._reap()

    async def close(self):
        """Signal every active handler and join replies before process teardown."""
        from lib.agent_core.native_process import _joined
        if self.pending:
            for callback in self.pending.values():
                callback.stopped.set()
            try:
                try:
                    self.process.stdin.write(self.client._encode(
                        {'version': self.client.protocol_version, 'type': 'cancel'}, self.client.budget.frame_bytes))
                    async with asyncio.timeout(self.client.budget.shutdown_seconds):
                        await self.process.stdin.drain()
                except (OSError, TimeoutError):
                    pass
            finally:
                await _joined(asyncio.gather(*(item.work for item in self.pending.values()), return_exceptions=True))
                self.pending.clear()
