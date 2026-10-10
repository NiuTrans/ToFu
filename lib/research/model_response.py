"""Exact model replies bound to admitted Research calls before tool progression.

ModelResponses shares private task-local material chunks with RequestContexts.
Reply manifests acknowledge complete message/finish material independently of
usage delivery; inspection never executes stored tool calls or approves science.
"""
from copy import deepcopy

from lib.storage.errors import StorageError
from .request_context import RequestContexts
from .model_accounting import ModelCalls
from .action_observations import _validate as _validate

NAMESPACE = 'research.model_responses.v1'


class ModelResponses(RequestContexts):
    def save_response(self, intent, message, finish):
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        if calls is None or calls.load_model_intent(intent['ordinal']) != intent or intent['binding'] != self.binding:
            raise StorageError('database_integrity', 'Research response does not match model admission')
        digest, chars, parts = self._write_material({'message': message, 'finish': finish}, 'responseMaterial')
        manifest = {'schema_version': 1, 'binding': deepcopy(self.binding),
            'ordinal': intent['ordinal'], 'admission_id': intent['admission_id'],
            'response_digest': digest, 'response_chars': chars, 'parts': parts}
        self._put(NAMESPACE, self.prefix + str(intent['ordinal']), manifest, 'modelResponse')
        return digest

    def load_response(self, ordinal):
        calls = ModelCalls.open(self.client, user_id=self.binding['user_id'], task_id=self.binding['task_id'])
        if calls is None:
            return None
        intent = calls.load_model_intent(ordinal)
        if intent is None:
            return None
        usage = calls.load_model_usage(ordinal)
        manifest = self._get(NAMESPACE, self.prefix + str(ordinal), 'modelResponse')
        if manifest is None:
            if usage is not None and usage.get('response_digest') is not None:
                raise StorageError('database_integrity', 'Acknowledged Research model response is missing')
            return None
        if (manifest['ordinal'] != ordinal or manifest['admission_id'] != intent['admission_id']
                or len(manifest['parts']) != (manifest['response_chars'] + self.part_chars - 1) // self.part_chars
                or (usage is not None and usage.get('response_digest') != manifest['response_digest'])):
            raise StorageError('database_integrity', 'Research response manifest identity is invalid')
        return self._read_material(manifest['response_digest'], manifest['response_chars'],
                                   manifest['parts'], 'responseMaterial')
