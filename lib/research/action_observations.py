"""Immutable Research tool observations on the configured record authority.

Entry: ActionObservations.capture/open/page/read; capture_action_observation connects the
action callback. Chunk ACKs precede manifest publication and model projection.
Owner/task/revision/basis identities fence reads. This journal preserves observed
bytes, not scientific verdicts or permission to replay external effects.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

import jsonschema

from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .persistence import research_direction_hash

INTENT_NAMESPACE = 'research.action_observation_intents.v1'
BASIS_NAMESPACE = 'research.action_observation_basis.v1'
BINDING_NAMESPACE = 'research.action_observation_bindings.v1'
NAMESPACE = 'research.action_observations.v1'
PART_NAMESPACE = 'research.action_observation_parts.v1'


@lru_cache(maxsize=20)
def _validator(kind):
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
        'research_action_observation_v1.schema.json').read_text(encoding='utf-8'))
    projected = {'$schema': schema['$schema'], '$defs': schema['$defs'],
                 '$ref': f'#/$defs/{kind}'}
    jsonschema.Draft202012Validator.check_schema(projected)
    return jsonschema.Draft202012Validator(projected)


def _validate(value, kind):
    try:
        _validator(kind).validate(value)
    except jsonschema.ValidationError as exc:
        raise ValueError(f'research observation {kind}: {exc.message[:200]}') from exc


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _task_prefix(user_id, task_id):
    uid = require_user_id(user_id, context='research observation owner')
    task_schema = _validator('binding').schema['$defs']['binding']['properties']['task_id']
    try:
        jsonschema.Draft202012Validator(task_schema).validate(task_id)
    except jsonschema.ValidationError as exc:
        raise ValueError('Invalid Research observation task identity') from exc
    return uid, f"{uid}:{_digest({'task_id': task_id})}:"


def _record_value(row, kind):
    if not isinstance(row, dict) or type(row.get('version')) is not int or row['version'] != 1:
        raise StorageError('database_integrity', 'Research observation version is invalid')
    value = row.get('value')
    try:
        _validate(value, kind)
    except ValueError as exc:
        raise StorageError('database_integrity', 'Research observation record is invalid') from exc
    return value


class ActionObservations:
    def __init__(self, client, *, user_id, task_id, direction, lang, revision,
                 basis_digest):
        self.client = client
        self.binding = {'user_id': require_user_id(user_id, context='research observation owner'),
            'task_id': task_id, 'direction_hash': research_direction_hash(direction),
            'lang': lang, 'revision': revision, 'basis_digest': basis_digest}
        _validate(self.binding, 'binding')
        _, self.prefix = _task_prefix(user_id, task_id)
        self.part_chars = _validator('part').schema['$defs']['part']['properties']['content']['maxLength']

    @classmethod
    def open(cls, client, *, user_id, task_id):
        """Recover the acknowledged binding without a live task or workspace.

        No current workspace, tool executor, or model is consulted. Opening a
        binding alone does not establish that any result manifest was published.
        """
        uid, prefix = _task_prefix(user_id, task_id)
        row = client.query('record.get', {'namespace': BINDING_NAMESPACE, 'key': prefix})
        if row is None:
            return None
        binding = _record_value(row, 'admission')['binding']
        if binding['user_id'] != uid or binding['task_id'] != task_id:
            raise StorageError('database_integrity', 'Research observation recovery identity is invalid')
        repository = cls.__new__(cls)
        repository.client = client
        repository.binding = deepcopy(binding)
        repository.prefix = prefix
        repository.part_chars = _validator('part').schema['$defs']['part']['properties']['content']['maxLength']
        return repository

    def page(self, *, limit=8, cursor=''):
        """Bounded manifest discovery; full original verification remains in read.

        Cursors follow storage key order, which is lexical rather than numeric.
        Missing ordinals are not filled in and never imply an effect can replay.
        """
        _validate_page_parameters(self.prefix, limit, cursor)
        rows = self.client.query('record.list', {'namespace': NAMESPACE,
            'prefix': self.prefix, 'after_key': cursor, 'limit': limit + 1})
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise StorageError('database_integrity', 'Research observation page is invalid')
        manifests = []
        previous_key = cursor
        for row in rows:
            value = _record_value(row, 'manifest')
            key = row.get('key')
            if (not isinstance(key, str) or key != self.prefix + str(value['ordinal'])
                    or key <= previous_key or value['binding'] != self.binding
                    or value['part_count'] != (value['result_chars'] + self.part_chars - 1) // self.part_chars
                    or value['receipt'].get('result_digest') != value['result_digest']):
                raise StorageError('database_integrity', 'Research observation page boundary is invalid')
            manifests.append(deepcopy(value))
            previous_key = key
        return {'observations': manifests[:limit],
            'next_cursor': rows[limit - 1]['key'] if len(rows) > limit else ''}

    def _get(self, namespace, key, kind):
        row = self.client.query('record.get', {'namespace': namespace, 'key': key})
        if row is None:
            return None
        value = _record_value(row, kind)
        if value['binding'] != self.binding:
            raise StorageError('database_integrity', 'Research observation binding is invalid')
        return value

    def _put(self, namespace, key, value, kind):
        _validate(value, kind)
        try:
            result = self.client.command('record.put', {'namespace': namespace, 'key': key,
                'value': value, 'expected_version': 0}, 'research-observation:' +
                _digest({'namespace': namespace, 'key': key, 'value': value}))
            if not isinstance(result, dict) or result.get('version') != 1:
                raise StorageError('database_internal', 'Research observation write was not acknowledged')
        except StorageError as exc:
            # Ambiguous ACKs and identical retries require exact immutable readback.
            if self._get(namespace, key, kind) != value:
                raise exc
            return
        if self._get(namespace, key, kind) != value:
            raise StorageError('database_integrity',
                               'Research immutable record was not exactly acknowledged')

    def freeze_basis(self, basis):
        """Acknowledge the exact declared basis before an experiment tool effect."""
        value = {'schema_version': 1, 'binding': deepcopy(self.binding), 'basis': deepcopy(basis)}
        _validate(value, 'frozenBasis')
        if _digest(basis) != self.binding['basis_digest']:
            raise ValueError('Research observation basis does not match its frozen binding')
        self._put(BINDING_NAMESPACE, self.prefix,
            {'schema_version': 1, 'binding': deepcopy(self.binding)}, 'admission')
        self._put(BASIS_NAMESPACE, self.prefix, value, 'frozenBasis')

    def load_basis(self):
        """Legacy missing snapshots remain missing; current workspace is never substituted."""
        value = self._get(BASIS_NAMESPACE, self.prefix, 'frozenBasis')
        if value is None:
            return None
        if self._get(BINDING_NAMESPACE, self.prefix, 'admission') is None:
            raise StorageError('database_integrity', 'Research observation basis binding is missing')
        if _digest(value['basis']) != self.binding['basis_digest']:
            raise StorageError('database_integrity', 'Research observation frozen basis digest is invalid')
        return deepcopy(value['basis'])

    def record_intent(self, *, ordinal, call_id, tool, arguments):
        """ACK before an effect; this never establishes execution or replay rights."""
        value = {'schema_version': 1, 'binding': deepcopy(self.binding), 'ordinal': ordinal,
                 'call_id': call_id, 'tool': tool, 'arguments_digest': _digest(arguments),
                 'admission_id': uuid.uuid4().hex}
        _validate(value, 'effectIntent')
        if self.load_basis() is None:
            raise ValueError('Research effect intent requires an acknowledged frozen basis')
        if self.load_intent(ordinal) is not None:
            raise StorageError('database_conflict', 'Research effect slot was already admitted; replay is refused')
        self._put(INTENT_NAMESPACE, self.prefix + str(ordinal), value, 'effectIntent')

    def load_intent(self, ordinal):
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError('Research effect intent ordinal must be positive')
        value = self._get(INTENT_NAMESPACE, self.prefix + str(ordinal), 'effectIntent')
        if value is not None and value['ordinal'] != ordinal:
            raise StorageError('database_integrity', 'Research effect intent ordinal is invalid')
        return deepcopy(value)

    def effects_page(self, *, limit=8, cursor=''):
        """Discover fenced unknown effects without claiming original verification."""
        _validate_page_parameters(self.prefix, limit, cursor)
        rows = self.client.query('record.list', {'namespace': INTENT_NAMESPACE,
            'prefix': self.prefix, 'after_key': cursor, 'limit': limit + 1})
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise StorageError('database_integrity', 'Research effect intent page is invalid')
        if rows and self.load_basis() is None:
            raise StorageError('database_integrity', 'Research effect intent frozen basis is missing')
        effects = []; previous_key = cursor
        for row in rows:
            intent = _record_value(row, 'effectIntent'); key = row.get('key')
            if (key != self.prefix + str(intent['ordinal']) or key <= previous_key
                    or intent['binding'] != self.binding):
                raise StorageError('database_integrity', 'Research effect intent page boundary is invalid')
            previous_key = key
            manifest = self._get(NAMESPACE, key, 'manifest')
            if manifest is not None and (manifest['ordinal'] != intent['ordinal']
                    or manifest['call_id'] != intent['call_id'] or manifest['tool'] != intent['tool']
                    or manifest['part_count'] != (manifest['result_chars'] + self.part_chars - 1) // self.part_chars
                    or manifest['receipt'].get('result_digest') != manifest['result_digest']):
                raise StorageError('database_integrity', 'Research effect observation identity is invalid')
            effect = {'intent': deepcopy(intent),
                'state': 'observation_published' if manifest is not None else 'unresolved_effect',
                'result_digest': manifest['result_digest'] if manifest is not None else None,
                'tool_status': manifest['receipt'].get('status') if manifest is not None else None,
                'replay_allowed': False}
            try:
                _validate(effect, 'effectState')
            except ValueError as exc:
                raise StorageError('database_integrity', 'Research effect state is invalid') from exc
            effects.append(effect)
        return {'effects': effects[:limit],
            'next_cursor': rows[limit - 1]['key'] if len(rows) > limit else ''}

    def capture(self, *, ordinal, call_id, tool, content, receipt):
        intent = self.load_intent(ordinal)
        if intent is not None and (intent['call_id'] != call_id or intent['tool'] != tool):
            raise StorageError('database_integrity', 'Research observation does not match its effect intent')
        if not isinstance(content, str):
            raise ValueError('Research observation content must be text')
        digest = hashlib.sha256()
        for offset in range(0, len(content), self.part_chars):
            digest.update(content[offset:offset + self.part_chars].encode('utf-8'))
        result_digest = digest.hexdigest()
        if receipt.get('result_digest') != result_digest:
            raise ValueError('Research observation receipt does not match original bytes')
        manifest = {'schema_version': 1, 'binding': deepcopy(self.binding),
            'ordinal': ordinal, 'call_id': call_id, 'tool': tool,
            'result_digest': result_digest, 'result_chars': len(content),
            'part_count': (len(content) + self.part_chars - 1) // self.part_chars,
            'receipt': deepcopy(receipt)}
        _validate(manifest, 'manifest')
        key = self.prefix + str(ordinal)
        prior = self._get(NAMESPACE, key, 'manifest')
        if prior is not None and prior != manifest:
            raise StorageError('database_conflict', 'Research observation ordinal is already bound')
        self._put(BINDING_NAMESPACE, self.prefix,
            {'schema_version': 1, 'binding': deepcopy(self.binding)}, 'admission')
        for part, offset in enumerate(range(0, len(content), self.part_chars)):
            value = {'schema_version': 1, 'binding': deepcopy(self.binding),
                'ordinal': ordinal, 'result_digest': result_digest, 'part': part,
                'content': content[offset:offset + self.part_chars]}
            self._put(PART_NAMESPACE, f'{key}:{result_digest}:{part}', value, 'part')
        self._put(NAMESPACE, key, manifest, 'manifest')
        return deepcopy(manifest)

    def _original_manifest(self, ordinal):
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError('Research observation ordinal must be positive')
        key = self.prefix + str(ordinal)
        manifest = self._get(NAMESPACE, key, 'manifest')
        if manifest is None:
            return None
        admission = self._get(BINDING_NAMESPACE, self.prefix, 'admission')
        if admission is None:
            raise StorageError('database_integrity', 'Research observation basis binding is missing')
        count = (manifest['result_chars'] + self.part_chars - 1) // self.part_chars
        if manifest['ordinal'] != ordinal or manifest['part_count'] != count:
            raise StorageError('database_integrity', 'Research observation manifest boundary is invalid')
        return manifest

    def _original_parts(self, manifest):
        ordinal = manifest['ordinal']
        key = self.prefix + str(ordinal)
        count = manifest['part_count']
        digest = hashlib.sha256()
        total = 0
        for part in range(count):
            value = self._get(PART_NAMESPACE, f"{key}:{manifest['result_digest']}:{part}", 'part')
            expected_length = min(self.part_chars, manifest['result_chars'] - total)
            if (value is None or value['ordinal'] != ordinal or value['part'] != part
                    or value['result_digest'] != manifest['result_digest']
                    or len(value['content']) != expected_length):
                raise StorageError('database_integrity', 'Research observation part is missing or invalid')
            text = value['content']
            digest.update(text.encode('utf-8'))
            total += len(text)
            yield text
        if (digest.hexdigest() != manifest['result_digest']
                or manifest['receipt'].get('result_digest') != manifest['result_digest']):
            raise StorageError('database_integrity', 'Research observation original digest is invalid')

    def read(self, ordinal):
        manifest = self._original_manifest(ordinal)
        if manifest is None:
            return None
        return deepcopy(manifest), ''.join(self._original_parts(manifest))

    def prepare_original_export(self, ordinal):
        """Return verified UTF-8 bytes in a temporary file; caller owns close.

        Verification finishes before ownership transfers. Failure closes the
        file, and no partly verified original is offered to a transport.
        """
        manifest = self._original_manifest(ordinal)
        if manifest is None:
            return None
        spool = tempfile.TemporaryFile(mode='w+b')
        try:
            for text in self._original_parts(manifest):
                spool.write(text.encode('utf-8'))
            byte_count = spool.tell()
            spool.seek(0)
            return deepcopy(manifest), spool, byte_count
        except BaseException:
            spool.close()
            raise


def action_observation_basis(workspace):
    """One scientific-basis projection for admission and continuation checks."""
    basis = {'hypothesis': workspace.get('hypothesis'), 'protocol': workspace.get('protocol')}
    if workspace.get('selected_idea_source_task_id'):
        basis.update({key: workspace.get(key) for key in
            ('selected_idea_id', 'selected_idea_source_task_id')})
    return basis


def freeze_action_observation_basis(task, workspace):
    from lib.storage import get_storage_client
    basis = action_observation_basis(workspace)
    basis_digest = _digest(basis)
    repository = ActionObservations(get_storage_client(write=True),
        user_id=task['user_id'], task_id=task['task_id'], direction=task['direction'],
        lang=workspace['lang'], revision=workspace['revision'], basis_digest=basis_digest)
    repository.freeze_basis(basis)
    return repository


def capture_action_observation(task, workspace, round_entry, result):
    repository = freeze_action_observation_basis(task, workspace)
    return repository.capture(ordinal=round_entry['roundNum'],
        call_id=round_entry['toolCallId'], tool=round_entry['toolName'],
        content=str(result), receipt=round_entry['_researchReceipt'])


def _validate_page_parameters(prefix, limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 16:
        raise ValueError('Research observation page limit must be 1..16')
    if (not isinstance(cursor, str) or len(cursor) > 512
        or (cursor and (not cursor.startswith(prefix)
            or not cursor[len(prefix):].isascii()
            or not cursor[len(prefix):].isdigit()
            or cursor[len(prefix):].startswith('0')))):
        raise ValueError('Invalid Research observation page cursor')


def load_action_observation_page(task_id, *, user_id, limit=8, cursor=''):
    """Owner-scoped read query for HTTP recovery after the live task is gone."""
    from lib.storage import get_storage_client
    _, prefix = _task_prefix(user_id, task_id)
    _validate_page_parameters(prefix, limit, cursor)
    repository = ActionObservations.open(get_storage_client(), user_id=user_id, task_id=task_id)
    if repository is None:
        return {'found': False, 'binding': None, 'observations': [], 'next_cursor': ''}
    return {'found': True, 'binding': deepcopy(repository.binding), **repository.page(limit=limit, cursor=cursor)}


def prepare_action_observation_export(task_id, ordinal, *, user_id):
    from lib.storage import get_storage_client
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError('Research observation ordinal must be positive')
    repository = ActionObservations.open(get_storage_client(), user_id=user_id, task_id=task_id)
    return repository.prepare_original_export(ordinal) if repository is not None else None


def load_action_effect_page(task_id, *, user_id, limit=8, cursor=''):
    from lib.storage import get_storage_client
    _, prefix = _task_prefix(user_id, task_id)
    _validate_page_parameters(prefix, limit, cursor)
    repository = ActionObservations.open(get_storage_client(), user_id=user_id, task_id=task_id)
    if repository is None:
        return {'found': False, 'binding': None, 'effects': [], 'next_cursor': ''}
    return {'found': True, 'binding': deepcopy(repository.binding), **repository.effects_page(limit=limit, cursor=cursor)}
