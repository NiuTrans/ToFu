"""Demand-owned artifact leases over anonymous temporary files.

Independent cloud requests start immediately; duplicate request identities
share one result. The host scheduler owns subscribers and retries. Identity and lease tokens scope every artifact access.
Anonymous files are removed by the OS even when the MCP process is killed.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import secrets
import tempfile
import time
from typing import BinaryIO

from .contract import ExportError, limit, validate
from .main_file import compiled_main_file


@dataclass
class _Entry:
    identity: dict
    expected_main: str | None = None
    token: str = field(default_factory=lambda: secrets.token_hex(32))
    files: dict[str, BinaryIO] = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    size: int = 0
    expires: float = 0
    manifest: dict | None = None
    task: asyncio.Task | None = None
    completion: asyncio.Future | None = None

    def close(self):
        for stream in self.files.values():
            # Buffered close can report a disk/flush error after closing its
            # descriptor. One such failure must not retain the other artifacts.
            with suppress(OSError):
                stream.close()


class ExportStore:
    def __init__(self, transport, *, clock=time.monotonic, cache_bytes=None,
                 compile_seconds=None, file_factory=tempfile.TemporaryFile):
        self.transport = transport
        self.clock = clock
        self.file_factory = file_factory
        # Retained for old embedding callers; this hint grants no quota or
        # refusal. Real anonymous-file errors remain ordinary export failures.
        if cache_bytes is not None and (type(cache_bytes) is not int or cache_bytes < 0):
            raise ExportError('export_configuration')
        self.compile_seconds = compile_seconds
        if compile_seconds is not None and (type(compile_seconds) not in (int, float)
                or not math.isfinite(compile_seconds) or compile_seconds <= 0):
            raise ExportError('export_configuration')
        self.entries: dict[tuple, _Entry] = {}
        self.closed = False
        self.retirement: asyncio.Task | None = None

    @staticmethod
    def _key(value):
        return value['owner_user_id'], value['project_id'], value['request_id']

    @staticmethod
    def _identity(value):
        return {key: value[key] for key in ('schema_version', 'owner_user_id', 'project_id', 'request_id')}

    def _prune(self):
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if entry.manifest is not None and entry.expires <= now:
                entry.close()
                del self.entries[key]

    async def __aenter__(self):
        async def retire():
            while True:
                await asyncio.sleep(min(30, limit('mcp_export_lease_seconds')))
                self._prune()
        self.retirement = asyncio.create_task(retire(), name='paper-artifact-leases')
        return self

    async def __aexit__(self, *_args):
        await self.aclose()

    async def aclose(self):
        self.closed = True
        tasks = {entry.task for entry in self.entries.values() if entry.task is not None}
        if self.retirement is not None:
            tasks.add(self.retirement)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Producers are quiescent before their borrowed streams are closed.
        for entry in self.entries.values():
            entry.close()
        self.entries.clear()

    def _manifest(self, entry):
        result = json.loads(json.dumps(entry.manifest))
        result['expires_in_seconds'] = max(1, math.ceil(entry.expires - self.clock()))
        return validate(result, 'PaperMcpManifestV1')

    async def build(self, request: dict) -> dict:
        validate(request, 'PaperMcpBuildRequestV1')
        self._prune()
        if self.closed:
            raise ExportError('export_closed')
        key = self._key(request)
        previous = self.entries.get(key)
        if previous is not None:
            if previous.expected_main != request.get('main_file'):
                raise ExportError('export_request_mismatch')
            if previous.manifest is None:
                await asyncio.shield(previous.completion)
                if self.closed or self.entries.get(key) is not previous:
                    raise ExportError('export_lease_missing')
            return self._manifest(previous)
        completion = asyncio.get_running_loop().create_future()
        # Retain failure for duplicate awaiters without an unobserved-Future
        # warning when a request has only its original caller.
        completion.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        entry = _Entry(self._identity(request), expected_main=request.get('main_file'),
                       task=asyncio.current_task(), completion=completion)
        self.entries[key] = entry

        async def write_artifact(name, chunks):
            if self.closed:
                raise ExportError('export_closed')
            if name not in ('document.pdf', 'document.synctex.gz', 'build.log') or name in entry.files:
                raise ExportError('export_artifact')
            stream = self.file_factory(mode='w+b')
            entry.files[name] = stream
            digest = hashlib.sha256()
            size = 0
            async for chunk in chunks:
                if self.closed:
                    raise ExportError('export_closed')
                if not isinstance(chunk, bytes) or len(chunk) > limit('transfer_chunk_bytes'):
                    raise ExportError('export_artifact_limit')
                stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                entry.size += len(chunk)
            if not size:
                raise ExportError('export_artifact_empty')
            stream.flush()
            entry.artifacts.append({'name': name, 'size_bytes': size, 'sha256': digest.hexdigest()})

        try:
            async with asyncio.timeout(self.compile_seconds):
                build_id, status = await self.transport.compile(request['project_id'], write_artifact)
            if self.closed:
                raise ExportError('export_closed')
            if status == 'success' and 'document.pdf' not in entry.files:
                raise ExportError('export_artifact')
            if status == 'failed' and 'document.pdf' in entry.files:
                raise ExportError('export_artifact')
            main_file = None
            try:
                log = entry.files.get('build.log')
                if log is not None:
                    log.seek(0)
                    main_file = compiled_main_file(log)
            except ExportError:
                if status == 'success':
                    raise
            if status == 'success' and main_file is None:
                raise ExportError('overleaf_main_file_unavailable')
            if entry.expected_main and main_file is not None and main_file != entry.expected_main:
                raise ExportError('overleaf_main_file_changed')
            entry.expires = self.clock() + limit('mcp_export_lease_seconds')
            entry.manifest = validate({**entry.identity, 'lease_token': entry.token,
                'remote_build_id': build_id, 'compile_status': status, 'version_verified': False,
                'main_file': main_file,
                'source_digest': None, 'expires_in_seconds': limit('mcp_export_lease_seconds'),
                'artifacts': entry.artifacts}, 'PaperMcpManifestV1')
            entry.task = None
            completion.set_result(None)
            return self._manifest(entry)
        except BaseException as error:
            original_error = error
            if isinstance(error, OSError) and not isinstance(error, TimeoutError):
                error = ExportError('export_storage_unavailable')
                error.__cause__ = original_error
            if self.entries.get(key) is entry:
                self.entries.pop(key, None)
            entry.close()
            if not completion.done():
                if isinstance(error, asyncio.CancelledError):
                    completion.cancel()
                else:
                    completion.set_exception(error)
            if error is not original_error:
                raise error from original_error
            raise

    def _lookup(self, request):
        self._prune()
        entry = self.entries.get(self._key(request))
        if (self.closed or entry is None or entry.manifest is None
                or not hmac.compare_digest(entry.token, request['lease_token'])):
            raise ExportError('export_lease_missing')
        return entry

    def read(self, request: dict) -> dict:
        validate(request, 'PaperMcpReadRequestV1')
        entry = self._lookup(request)
        name, offset = request['artifact'], request['offset']
        artifact = next((row for row in entry.artifacts if row['name'] == name), None)
        if artifact is None or offset > artifact['size_bytes']:
            raise ExportError('export_artifact_range')
        stream = entry.files[name]
        stream.seek(offset)
        chunk = stream.read(limit('transfer_chunk_bytes'))
        return validate({**entry.identity, 'artifact': name, 'offset': offset,
                         'data': base64.b64encode(chunk).decode('ascii'),
                         'eof': offset + len(chunk) == artifact['size_bytes']}, 'PaperMcpChunkV1')

    def release(self, request: dict) -> dict:
        validate(request, 'PaperMcpLeaseV1')
        try:
            entry = self._lookup(request)
        except ExportError:
            released = False
        else:
            self.entries.pop(self._key(request))
            entry.close()
            released = True
        return validate({**self._identity(request), 'released': released}, 'PaperMcpReleaseResultV1')
