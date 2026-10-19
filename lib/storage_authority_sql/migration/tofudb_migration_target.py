"""Exclusive offline Tofu-DB child session; no listener or live credentials.

The operator supplies an existing authority UUID and explicit scope. Generated
frame bounds and absolute deadlines cover both pipe directions. A transport
failure closes this owned child; resume always opens and verifies durable state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time
from typing import Any
from uuid import UUID

from lib.storage_authority_sql.migration.generated_tofudb_migration import OPERATOR_SESSION
from tofu_db.wire import StorageV2Error


class MigrationTargetError(ValueError):
    """Private operator transport, identity or resource admission failed."""


class _Response:
    def __init__(self, envelope: dict[str, Any]):
        self._envelope = envelope

    def json(self) -> Any:
        return self._envelope.get('value')

    def raise_for_error(self) -> None:
        if not self._envelope['ok']:
            raise StorageV2Error(9, self._envelope['error_code'],
                                 self._envelope['message'], False)


class OfflineMigrationSession:
    def __init__(self, binary: Path, target: Path, authority_uuid: str, *,
                 owner_id: int, tenant_id: int, timeout: float = 15.0):
        if (not Path(binary).is_absolute() or not Path(target).is_absolute()
                or type(owner_id) is not int or not 0 < owner_id < 2**64
                or type(tenant_id) is not int or not 0 < tenant_id < 2**64
                or not 0 < timeout <= 120):
            raise MigrationTargetError('migration requires absolute paths and positive scope')
        self.authority_uuid = str(UUID(authority_uuid))
        self.owner_id, self.tenant_id = owner_id, tenant_id
        self._timeout = timeout
        self._sequence = 0
        self._closed = False
        self._pending = bytearray()
        # The native command emits failures as bounded protocol envelopes. Its
        # terminal diagnostic is intentionally not retained as an unbounded log;
        # the operator may redirect it to a bounded file for offline diagnosis.
        stderr_target = subprocess.DEVNULL
        diag = os.environ.get('TOFU_MIGRATION_CHILD_STDERR', '').strip()
        if diag:
            stderr_target = open(diag, 'ab', buffering=0)
        self._process = subprocess.Popen(
            [str(binary), 'migration-session', '--data-dir', str(target),
             '--owner-id', str(owner_id), '--tenant-id', str(tenant_id), '--execute'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_target,
            bufsize=0,
        )
        try:
            for pipe in (self._process.stdin, self._process.stdout):
                os.set_blocking(pipe.fileno(), False)
            ready = self._read(time.monotonic() + timeout, 4096)
            if ready != {'format': OPERATOR_SESSION['format'],
                         'authority_uuid': self.authority_uuid,
                         'owner_id': owner_id, 'tenant_id': tenant_id}:
                raise MigrationTargetError('migration child authority identity or scope differs')
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> OfflineMigrationSession:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def _wait(pipe: Any, event: int, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('migration pipe deadline elapsed')
        with selectors.DefaultSelector() as selector:
            selector.register(pipe, event)
            if not selector.select(remaining):
                raise TimeoutError('migration pipe deadline elapsed')

    def _read(self, deadline: float, maximum: int) -> dict[str, Any]:
        while True:
            end = self._pending.find(b'\n')
            if end >= 0:
                if end + 1 > maximum:
                    raise MigrationTargetError('migration response exceeds frame bound')
                line = bytes(self._pending[:end])
                del self._pending[:end + 1]
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    raise MigrationTargetError('invalid migration response') from None
                if not isinstance(value, dict):
                    raise MigrationTargetError('invalid migration response envelope')
                return value
            if len(self._pending) >= maximum:
                raise MigrationTargetError('migration response exceeds frame bound')
            self._wait(self._process.stdout, selectors.EVENT_READ, deadline)
            chunk = os.read(self._process.stdout.fileno(), min(65536, maximum - len(self._pending)))
            if not chunk:
                raise MigrationTargetError('migration child exited or truncated its response')
            self._pending.extend(chunk)

    def request(self, operation: str, payload: dict[str, Any], *,
                command_id: str | None, deadline_unix_ms: int) -> _Response:
        if self._closed:
            raise MigrationTargetError('migration session is closed')
        deadline = time.monotonic() + min(
            self._timeout, max(0, deadline_unix_ms / 1000 - time.time()))
        self._sequence += 1
        envelope = {'request_id': self._sequence,
                    'target_authority_uuid': self.authority_uuid,
                    'operation': operation, 'payload': payload,
                    'command_id': command_id, 'deadline_unix_ms': deadline_unix_ms}
        maximum = OPERATOR_SESSION['maximumFrameBytes']
        frame = bytearray()
        try:
            encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            for chunk in encoder.iterencode(envelope):
                encoded = chunk.encode('utf-8')
                if len(encoded) >= maximum - len(frame):
                    raise MigrationTargetError('migration request exceeds frame bound')
                frame.extend(encoded)
            frame.append(10)
            view = memoryview(frame)
            while view:
                self._wait(self._process.stdin, selectors.EVENT_WRITE, deadline)
                try:
                    written = os.write(self._process.stdin.fileno(), view)
                except BlockingIOError:
                    continue
                view = view[written:]
            response = self._read(deadline, maximum)
            if (response.get('request_id') != self._sequence
                    or type(response.get('ok')) is not bool
                    or (not response['ok'] and (
                        not isinstance(response.get('error_code'), str)
                        or not isinstance(response.get('message'), str)))):
                raise MigrationTargetError('migration response correlation or status differs')
            return _Response(response)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        for pipe in (self._process.stdin, self._process.stdout):
            pipe.close()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2)
