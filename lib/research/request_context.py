"""Owner/task-bound exact request material, acknowledged before model dispatch.

Chunks are content-addressed within one task for prefix sharing across rounds.
Manifests become readable only through an admitted model intent. Reads validate
the complete canonical material and never grant replay, tools or budget credit.
Private material may contain user research data and user-supplied defaults;
provider transport headers are not part of this snapshot.
"""
from copy import deepcopy
import hashlib
import json

from lib.storage.errors import StorageError
from .action_observations import ActionObservations, _validate, _digest

NAMESPACE = 'research.request_contexts.v1'
PART_NAMESPACE = 'research.request_context_parts.v1'


class RequestContexts(ActionObservations):
    def _write_material(self, material, kind):
        _validate(material, kind)
        wire = json.dumps(material, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(); parts = []
        for offset in range(0, len(wire), self.part_chars):
            content = wire[offset:offset + self.part_chars]
            encoded = content.encode('utf-8'); digest.update(encoded)
            part_digest = hashlib.sha256(encoded).hexdigest(); parts.append(part_digest)
            value = {'schema_version': 1, 'binding': deepcopy(self.binding),
                     'content_digest': part_digest, 'content': content}
            self._put(PART_NAMESPACE, self.prefix + part_digest, value, 'requestContextPart')
        return digest.hexdigest(), len(wire), parts

    def save_request(self, material):
        _validate(material, 'requestMaterial')
        definition = material.get('action_definition')
        if definition is not None:
            from .persistence import research_direction_hash
            if (definition['lang'] != self.binding['lang']
                    or definition['expected_revision'] != self.binding['revision']
                    or research_direction_hash(definition['direction']) != self.binding['direction_hash']):
                raise StorageError('database_integrity', 'Research action definition does not match its frozen binding')
        from .model_accounting import INTENT_NAMESPACE
        first = self._get(INTENT_NAMESPACE, self.prefix + '1', 'modelIntent')
        if first is not None:
            original = self._get(NAMESPACE, self.prefix + first['request_digest'], 'requestContext')
            if original is None or original.get('action_definition') != definition:
                raise StorageError('database_conflict', 'Research action definition changed during execution')
        request_digest, request_chars, parts = self._write_material(material, 'requestMaterial')
        manifest = {'schema_version': 1, 'binding': deepcopy(self.binding),
                    'request_digest': request_digest, 'request_chars': request_chars, 'parts': parts}
        if definition is not None:
            manifest['action_definition'] = deepcopy(definition)
        self._put(NAMESPACE, self.prefix + request_digest, manifest, 'requestContext')
        return request_digest

    def load_action_definition(self, ordinal):
        """Read the acknowledged header for inspection, not execution admission.

        Continuation still loads and hashes full request material. This projection
        never returns private messages/tools or grants the header's write policy.
        """
        from .model_accounting import ModelCalls
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        intent = calls.load_model_intent(ordinal) if calls is not None else None
        if intent is None:
            return None
        manifest = self._get(NAMESPACE, self.prefix + intent['request_digest'], 'requestContext')
        if manifest is None:
            if intent.get('request_snapshot_version') == 1:
                raise StorageError('database_integrity', 'Acknowledged Research request snapshot is missing')
            return None
        if (manifest['request_digest'] != intent['request_digest']
                or len(manifest['parts']) != (manifest['request_chars'] + self.part_chars - 1) // self.part_chars):
            raise StorageError('database_integrity', 'Research request manifest boundary is invalid')
        return deepcopy(manifest.get('action_definition'))

    def load_request(self, ordinal):
        from .model_accounting import ModelCalls
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        if calls is None:
            return None
        if calls.binding != self.binding:
            raise StorageError('database_integrity', 'Research request model binding is invalid')
        intent = calls.load_model_intent(ordinal)
        if intent is None:
            return None
        manifest = self._get(NAMESPACE, self.prefix + intent['request_digest'], 'requestContext')
        if manifest is None:
            if intent.get('request_snapshot_version') == 1:
                raise StorageError('database_integrity', 'Acknowledged Research request snapshot is missing')
            return None  # Legacy intents have no invented request context.
        if (manifest['request_digest'] != intent['request_digest']
                or len(manifest['parts']) != (manifest['request_chars'] + self.part_chars - 1) // self.part_chars):
            raise StorageError('database_integrity', 'Research request manifest boundary is invalid')
        material = self._read_material(intent['request_digest'], manifest['request_chars'], manifest['parts'], 'requestMaterial')
        if material['max_output'] != intent['max_output']:
            raise StorageError('database_integrity', 'Research request output reservation is invalid')
        if material.get('action_definition') != manifest.get('action_definition'):
            raise StorageError('database_integrity', 'Research action definition differs from its acknowledged material')
        return material

    def _read_material(self, material_digest, material_chars, parts, kind):
        digest = hashlib.sha256(); pieces = []; chars = 0
        for part_digest in parts:
            value = self._get(PART_NAMESPACE, self.prefix + part_digest, 'requestContextPart')
            expected_length = min(self.part_chars, material_chars - chars)
            if (value is None or value['content_digest'] != part_digest
                    or len(value['content']) != expected_length
                    or hashlib.sha256(value['content'].encode('utf-8')).hexdigest() != part_digest):
                raise StorageError('database_integrity', 'Research request part is missing or invalid')
            content = value['content']; chars += len(content)
            digest.update(content.encode('utf-8')); pieces.append(content)
        if digest.hexdigest() != material_digest:
            raise StorageError('database_integrity', 'Research request original digest is invalid')
        try:
            material = json.loads(''.join(pieces)); _validate(material, kind)
        except (ValueError, TypeError, RecursionError) as exc:
            raise StorageError('database_integrity', 'Research request material is invalid') from exc
        if _digest(material) != material_digest:
            raise StorageError('database_integrity', 'Research request material does not match admission')
        return material
