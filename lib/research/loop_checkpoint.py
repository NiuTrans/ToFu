"""Research completed-round material bound to acknowledged model accounting.

LoopCheckpoints uses private task-scoped request chunks and immutable manifests.
A read restores data, not task ownership or scientific approval; it never runs
stored tools or recalls the provider. The shared chassis validates its own state.
"""
from copy import deepcopy

from lib.storage.errors import StorageError
from .request_context import RequestContexts
from .model_accounting import ModelCalls
from .action_observations import _validate

NAMESPACE = 'research.loop_checkpoints.v1'


class LoopCheckpoints(RequestContexts):
    def _validate_accounted_material(self, intent, material):
        _validate(material, 'loopMaterial')
        from lib.agent_loop import validate_agent_loop_checkpoint
        try:
            validate_agent_loop_checkpoint(material['chassis'], require_continuable=False)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageError('database_integrity', 'Research loop checkpoint has invalid chassis state') from exc
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        usage = calls.load_model_usage(intent['ordinal']) if calls is not None else None
        if (usage is None or material['usage_snapshot'] != usage['usage_snapshot']
                or material['chassis']['next_round'] != intent['round_index'] + 1):
            raise StorageError('database_integrity', 'Research loop checkpoint does not match accounted round')
        claimed = set(material['chassis']['claimed_tool_call_ids'])
        completed = set()
        for position, entry in enumerate(material['tool_rounds'], 1):
            if (type(entry.get('roundNum')) is not int or entry['roundNum'] != position
                    or type(entry.get('llmRound')) is not int or not 0 <= entry['llmRound'] <= intent['round_index']
                    or entry.get('status') not in ('done', 'rejected')):
                raise StorageError('database_integrity', 'Research loop checkpoint contains unsettled tools')
            call_id = entry.get('toolCallId')
            if not isinstance(call_id, str) or not call_id or call_id in completed or call_id not in claimed:
                raise StorageError('database_integrity', 'Research loop checkpoint lost its tool call identity fence')
            completed.add(call_id)

    def save_checkpoint(self, intent, material):
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        if calls is None or calls.load_model_intent(intent['ordinal']) != intent or intent['binding'] != self.binding:
            raise StorageError('database_integrity', 'Research checkpoint does not match model admission')
        calls.restore_usage_meter(intent['ordinal'])  # complete accounting; no successor may already be admitted
        self._validate_accounted_material(intent, material)
        digest, chars, parts = self._write_material(material, 'loopMaterial')
        manifest = {'schema_version': 1, 'binding': deepcopy(self.binding),
            'ordinal': intent['ordinal'], 'admission_id': intent['admission_id'],
            'material_digest': digest, 'material_chars': chars, 'parts': parts}
        self._put(NAMESPACE, self.prefix + str(intent['ordinal']), manifest, 'loopCheckpoint')
        return digest

    def load_checkpoint(self, ordinal):
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        intent = calls.load_model_intent(ordinal) if calls is not None else None
        if intent is None:
            return None
        manifest = self._get(NAMESPACE, self.prefix + str(ordinal), 'loopCheckpoint')
        if manifest is None:
            return None
        if (manifest['ordinal'] != ordinal or manifest['admission_id'] != intent['admission_id']
                or len(manifest['parts']) != (manifest['material_chars'] + self.part_chars - 1) // self.part_chars):
            raise StorageError('database_integrity', 'Research loop checkpoint identity is invalid')
        material = self._read_material(manifest['material_digest'], manifest['material_chars'],
                                       manifest['parts'], 'loopMaterial')
        self._validate_accounted_material(intent, material)
        return material

    def restore_checkpoint(self, ordinal, *, fallback_model=''):
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        if calls is None:
            raise StorageError('database_integrity', 'Research loop checkpoint admission is missing')
        meter = calls.restore_usage_meter(ordinal, fallback_model=fallback_model)
        material = self.load_checkpoint(ordinal)
        if material is None:
            raise StorageError('database_integrity', 'Research completed-round checkpoint is missing')
        return material, meter
