"""Conversation cancellation attribution on the existing durable control port.

Entry: encode_cancellation at Stop, decode_cancellation at the claimed worker.
The worker-job authority retains the first opaque reason. This versioned domain
envelope preserves the same cause/provenance as local TaskRuntime cancellation;
it adds no storage, polling loop or second cancellation authority.
"""
import json

from lib.durable_worker import WorkerProtocolError

_CONTRACT = 'tofu.conversation-cancel/v1'


def encode_cancellation(reason='', detail=''):
    if not isinstance(reason, str) or not isinstance(detail, str):
        raise TypeError('conversation cancellation cause must be text')
    if not reason:
        return 'user_stop'
    return json.dumps({'contract': _CONTRACT, 'reason': reason, 'detail': detail},
                      ensure_ascii=False, separators=(',', ':'))


def decode_cancellation(value):
    if not isinstance(value, str):
        raise WorkerProtocolError('conversation cancellation cause must be text')
    if value in ('', 'user_stop'):
        return '', ''
    if not value.startswith('{'):
        return value, ''
    try:
        payload = json.loads(value)
    except ValueError as error:
        raise WorkerProtocolError('invalid conversation cancellation envelope') from error
    if (not isinstance(payload, dict) or payload.get('contract') != _CONTRACT
            or not isinstance(payload.get('reason'), str) or not payload['reason']
            or not isinstance(payload.get('detail'), str)):
        raise WorkerProtocolError('invalid conversation cancellation envelope')
    return payload['reason'], payload['detail']
