"""Standing instructions: owner text frozen from a genuine human Turn (docs/ROUTER.md R4).

Responsibility: create, read, page, pause, resume and cancel owner-scoped
instruction records and record every application. Only an active instruction
can be applied; pause and resume are owner actions (HTTP), cancellation is final. A record freezes the authored text of one
genuine human Turn of the Global Router byte for byte, with that source Turn,
an optional channel thread scope and an optional target project. Applying it
(route_to_project with instruction_id) forwards exactly that text; the source
Turn stays the authority and the triggering event travels as nonhuman evidence.

Storage: portable record CAS (namespaces in contracts/router_v1.schema.json).
One record per instruction and one per application, so application history has
no lifetime cap and no record grows. Pages follow the project dispatch history
page bound (get_project_workers convention).

Entry points: RouterInstructions(owner, client=None) with create, read, page,
pause, resume, cancel, record_application, applications; authored_text.
Dependencies: lib.router.contract, lib.storage (record.* operations).
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import time
import uuid

from lib.identity import require_user_id
from lib.storage.errors import StorageError

from .contract import NAMESPACES, validate

# What a replay must repeat. State, timestamps and the source projection revision
# (display-only enrichment advances it) may differ; the original record wins.
_FROZEN_FIELDS = ('owner', 'instructionId', 'routerConversationId', 'sourceTurnId',
                  'text', 'label', 'projectPath', 'thread')


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                          .encode('utf-8')).hexdigest()


def authored_text(turn: Mapping) -> str:
    """The owner's own words of a human Turn: the original before any input translation."""
    projection = turn.get('projection') if isinstance(turn, Mapping) else None
    projection = projection if isinstance(projection, Mapping) else {}
    original = projection.get('originalContent')
    text = original if isinstance(original, str) else projection.get('content')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('The source Turn has no text')
    return text


def _page_rows() -> int:
    from lib.project_dispatch.history import PAGE_ROWS
    return PAGE_ROWS


class RouterInstructions:
    """Owner-scoped instruction and application records."""

    def __init__(self, owner: int, *, client=None):
        self.owner = require_user_id(owner, context='router instructions')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.prefix = f'{self.owner}:'

    def _row(self, namespace: str, key: str, definition: str):
        row = self.client().query('record.get', {'namespace': namespace, 'key': key})
        if row is None:
            return None
        try:
            value = validate(row['value'], definition)
            if value['owner'] != self.owner or type(row['version']) is not int or row['version'] < 1:
                raise ValueError('Router record escaped its owner')
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError('database_integrity', 'Invalid router instruction record') from error
        return row

    def _put(self, namespace: str, key: str, value: dict, version: int, command: str):
        self.client(write=True).command('record.put', {
            'namespace': namespace, 'key': key, 'value': value, 'expected_version': version,
        }, command)

    def _key(self, instruction_id: str) -> str:
        if not isinstance(instruction_id, str) or not instruction_id.strip() or len(instruction_id) > 256:
            raise ValueError('instruction_id is required')
        return self.prefix + instruction_id

    def read(self, instruction_id: str) -> dict:
        row = self._row(NAMESPACES['instructions'], self._key(instruction_id), 'instruction')
        if row is None:
            raise ValueError('Unknown standing instruction')
        return deepcopy(row['value'])

    def create(self, *, router_conversation_id: str, source_turn: Mapping, label: str,
               project_path: str | None, thread: str | None, idempotency_key: str) -> dict:
        """Freeze the source Turn's authored text; replay returns the original record."""
        source_turn_id = source_turn.get('turnId')
        instruction_id = 'ri-' + _digest([self.owner, router_conversation_id, source_turn_id,
                                          idempotency_key])[:32]
        value = validate({
            'version': 1, 'owner': self.owner, 'instructionId': instruction_id, 'state': 'active',
            'routerConversationId': router_conversation_id, 'sourceTurnId': source_turn_id,
            'sourceProjectionRevision': source_turn.get('projectionRevision'),
            'text': authored_text(source_turn), 'label': label, 'projectPath': project_path,
            'thread': thread, 'createdAt': int(time.time() * 1000), 'cancelledAt': None,
        }, 'instruction')
        key = self._key(instruction_id)
        existing = self._row(NAMESPACES['instructions'], key, 'instruction')
        if existing is None:
            try:
                self._put(NAMESPACES['instructions'], key, value, 0,
                          'router-instruction:' + _digest([key, value['createdAt']]))
                return value
            except StorageError as error:
                # A concurrent creator won the create-only CAS: compare with it.
                if error.code != 'database_conflict':
                    raise
                existing = self._row(NAMESPACES['instructions'], key, 'instruction')
                if existing is None:
                    raise
        if any(existing['value'][name] != value[name] for name in _FROZEN_FIELDS):
            raise ValueError('idempotency_key was reused for a different instruction')
        return deepcopy(existing['value'])

    def _transition(self, instruction_id: str, command: str, change) -> dict:
        """CAS one state change; ``change`` returns the next value, or None when already there."""
        key = self._key(instruction_id)
        while True:
            row = self._row(NAMESPACES['instructions'], key, 'instruction')
            if row is None:
                raise ValueError('Unknown standing instruction')
            value = change(deepcopy(row['value']))
            if value is None:
                return deepcopy(row['value'])
            try:
                self._put(NAMESPACES['instructions'], key, validate(value, 'instruction'),
                          row['version'], f'router-instruction-{command}:' + uuid.uuid4().hex)
                return value
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise

    def cancel(self, instruction_id: str) -> dict:
        """Cancel future applications; already forwarded work keeps its own authority."""
        return self._transition(instruction_id, 'cancel', lambda value: None if value['state'] == 'cancelled'
                                else {**value, 'state': 'cancelled', 'cancelledAt': int(time.time() * 1000)})

    def pause(self, instruction_id: str) -> dict:
        """Stop applying it until the owner resumes it; forwarded work keeps its authority."""
        def change(value):
            if value['state'] == 'cancelled':
                raise ValueError('A cancelled standing instruction cannot be paused')
            if value['state'] == 'paused':
                return None
            return {**value, 'state': 'paused', 'pausedAt': int(time.time() * 1000)}
        return self._transition(instruction_id, 'pause', change)

    def resume(self, instruction_id: str) -> dict:
        """Make a paused instruction applicable again; cancellation stays final."""
        def change(value):
            if value['state'] == 'cancelled':
                raise ValueError('A cancelled standing instruction cannot be resumed; ask the Global '
                                 'Router to record a new one')
            if value['state'] == 'active':
                return None
            return {**value, 'state': 'active', 'pausedAt': None}
        return self._transition(instruction_id, 'resume', change)

    def _list(self, namespace: str, prefix: str, cursor: str | None, definition: str):
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError('Invalid cursor')
        limit = _page_rows()
        rows = self.client().query('record.list', {
            'namespace': namespace, 'prefix': prefix,
            'after_key': prefix + cursor if cursor else '', 'limit': limit + 1})
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise StorageError('database_integrity', 'Invalid router record page')
        values = []
        for row in rows[:limit]:
            key = row.get('key') if isinstance(row, Mapping) else None
            if not isinstance(key, str) or not key.startswith(prefix):
                raise StorageError('database_integrity', 'Router record page escaped its owner')
            try:
                value = validate(row['value'], definition)
            except (KeyError, TypeError, ValueError) as error:
                raise StorageError('database_integrity', 'Invalid router record') from error
            if value['owner'] != self.owner:
                raise StorageError('database_integrity', 'Router record escaped its owner')
            values.append((key[len(prefix):], value))
        next_cursor = values[-1][0] if len(rows) > limit else None
        return values, next_cursor

    def page(self, *, cursor: str | None = None, include_cancelled: bool = False) -> dict:
        """One bounded page of active and paused records (and cancelled ones when asked).

        A filtered page may be short yet still carry a cursor.
        """
        values, next_cursor = self._list(NAMESPACES['instructions'], self.prefix, cursor, 'instruction')
        return {'instructions': [value for _, value in values
                                 if include_cancelled or value['state'] != 'cancelled'],
                'nextCursor': next_cursor}

    def record_application(self, application: dict) -> dict:
        """Record one application exactly once; a replay returns the original."""
        application = validate(application, 'instructionApplication')
        if application['owner'] != self.owner:
            raise PermissionError('Application belongs to another owner')
        key = self._key(application['instructionId']) + ':' + application['applicationId']
        row = self._row(NAMESPACES['applications'], key, 'instructionApplication')
        if row is not None:
            return deepcopy(row['value'])
        try:
            self._put(NAMESPACES['applications'], key, application, 0,
                      'router-instruction-application:' + _digest(key))
        except StorageError as error:
            if error.code != 'database_conflict':
                raise
            row = self._row(NAMESPACES['applications'], key, 'instructionApplication')
            if row is None:
                raise
            return deepcopy(row['value'])
        return application

    def applications(self, instruction_id: str, *, cursor: str | None = None) -> dict:
        instruction = self.read(instruction_id)
        values, next_cursor = self._list(NAMESPACES['applications'],
                                         self._key(instruction_id) + ':', cursor,
                                         'instructionApplication')
        return validate({'instruction': instruction, 'applications': [value for _, value in values],
                         'nextCursor': next_cursor}, 'instructionDetail')


__all__ = ['RouterInstructions', 'authored_text']
