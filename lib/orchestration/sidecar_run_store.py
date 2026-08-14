"""StorageClient adapter for durable orchestration run semantics."""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable

from lib.log import get_logger
from lib.orchestration.run_store_port import (
    OrchestrationRunStoreError,
    RunEventPage,
)
from lib.storage.client import StorageClient
from lib.storage.errors import StorageError


logger = get_logger(__name__)


class SidecarOrchestrationRunStore:
    def __init__(self, client: Callable[..., StorageClient] | None = None):
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self._client = client

    def _storage(self, *, write: bool = False) -> StorageClient:
        return self._client(write=write)

    @staticmethod
    def _command_id(prefix: str) -> str:
        return f'{prefix}:{uuid.uuid4().hex}'

    def _write(self, operation: str, payload: dict, *, receipt: bool) -> dict:
        try:
            return self._storage(write=True).command(
                operation,
                payload,
                self._command_id(operation) if receipt else None,
            )
        except StorageError as error:
            logger.warning(
                '[OrchRuns] storage write failed operation=%s code=%s',
                operation, error.code)
            return {}

    def _read(self, operation: str, payload: dict):
        try:
            return self._storage().query(operation, payload)
        except StorageError as error:
            logger.warning(
                '[OrchRuns] storage read failed operation=%s code=%s',
                operation, error.code)
            raise OrchestrationRunStoreError(
                f'orchestration storage read failed: {operation}') from error

    def new_run_id(self) -> str:
        return 'run_' + hex(int(time.time() * 1000))[2:] + secrets.token_hex(2)

    def create_run(self, run_id: str, *, definition: dict,
                   input_text: str = '', orch_id: str = '', name: str = '',
                   created_by: str = '') -> bool:
        if not run_id:
            return False
        result = self._write('orchestration.run.create', {
            'run_id': run_id,
            'definition': definition or {},
            'input': input_text or '',
            'orch_id': orch_id or '',
            'name': name or '',
            'created_by': created_by or '',
        }, receipt=True)
        return bool(result.get('created'))

    def update_status(self, run_id: str, status: str, *,
                      final: str | None = None,
                      error: dict | str | None = None) -> bool:
        if not run_id:
            return False
        payload = {'run_id': run_id, 'status': status}
        if final is not None:
            payload['final'] = final
        if error is not None:
            payload['error'] = error
        return bool(self._write(
            'orchestration.run.update_status', payload,
            receipt=True).get('changed'))

    def retire_interrupted_runs(self, error: dict | str) -> int | None:
        result = self._write(
            'orchestration.run.retire_interrupted', {'error': error},
            receipt=True)
        return int(result['retired']) if 'retired' in result else None

    def get_run(self, run_id: str) -> dict | None:
        if not run_id:
            return None
        return self._read('orchestration.run.get', {'run_id': run_id})

    def list_runs(self, *, status: str = '', orch_id: str = '',
                  limit: int = 50) -> list[dict]:
        return self._read('orchestration.run.list', {
            'status': status, 'orch_id': orch_id, 'limit': limit,
        })

    def append_event(self, run_id: str, seq: int, event: dict) -> bool:
        if not run_id or seq is None:
            return False
        return bool(self._write('orchestration.event.append', {
            'run_id': run_id, 'sequence': int(seq), 'event': event,
        }, receipt=False).get('accepted'))

    def project_event(self, run_id: str, seq: int, event: dict,
                      status: str = '') -> bool:
        if not run_id or seq is None:
            return False
        return bool(self._write('orchestration.event.project', {
            'run_id': run_id, 'sequence': int(seq), 'event': event,
            'status': status,
        }, receipt=False).get('projected'))

    def get_event_page(self, run_id: str, cursor: int = 0) -> RunEventPage:
        if not run_id:
            return RunEventPage([], 0, False, True)
        result = self._read('orchestration.event.page', {
            'run_id': run_id, 'cursor': max(0, int(cursor or 0)),
        })
        return RunEventPage(
            events=result['events'],
            next_cursor=int(result['next_cursor']),
            cursor_reset=bool(result['cursor_reset']),
            caught_up=bool(result['caught_up']),
        )

    def get_events(self, run_id: str, cursor: int = 0) -> list[dict]:
        requested = max(0, int(cursor or 0))
        collected = []
        while True:
            page = self.get_event_page(run_id, requested)
            if page.cursor_reset:
                return collected
            collected.extend(page.events)
            if page.caught_up or page.next_cursor <= requested:
                return collected
            requested = page.next_cursor

    def delete_run(self, run_id: str) -> bool:
        if not run_id:
            return False
        return bool(self._write(
            'orchestration.run.delete', {'run_id': run_id},
            receipt=True).get('deleted'))


__all__ = ['SidecarOrchestrationRunStore']
