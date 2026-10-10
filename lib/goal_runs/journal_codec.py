"""Lossless Goal journal packets within existing storage event boundaries.

Chunks share the run's owner, sequence stream and deletion lifecycle. Only the
final digest-bound reference grants a logical event; partial chunk writes grant
neither a completed effect nor launch authority.
"""
from __future__ import annotations

import base64
import hashlib
import json

from lib.goal_runs.contract import GOAL_EXECUTION_LAUNCH_FORMAT, GOAL_EXECUTION_LAUNCH_FORMATS
from lib.orchestration.execution_journal import RecoveryRefused

CHUNK_EVENT = 'execution_journal_payload_chunk'
CHUNK_BYTES = 512 * 1024


def encode_event(event):
    body = json.dumps(event, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(',', ':')).encode()
    if len(body) <= CHUNK_BYTES:
        yield event
        return
    digest = hashlib.sha256(body).hexdigest()
    count = (len(body) + CHUNK_BYTES - 1) // CHUNK_BYTES
    for index in range(count):
        yield {'type': CHUNK_EVENT, 'sha256': digest, 'index': index,
               'data': base64.b64encode(body[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]).decode('ascii')}
    yield {'type': event['type'], 'goal_execution_archive': GOAL_EXECUTION_LAUNCH_FORMAT,
           'payload_ref': {'sha256': digest, 'chunks': count, 'byte_length': len(body)}}


def decode_events(events):
    """Hydrate committed packets, refusing missing/reordered/foreign chunks."""
    chunks = {}
    for event in events:
        if event.get('type') == CHUNK_EVENT:
            digest, index = event.get('sha256'), event.get('index')
            if (not isinstance(digest, str) or len(digest) != 64
                    or type(index) is not int or index < 0):
                raise RecoveryRefused('Invalid Goal archive chunk identity')
            parts = chunks.setdefault(digest, [])
            if index != len(parts):
                raise RecoveryRefused('Goal archive chunks are missing or reordered')
            try:
                part = base64.b64decode(event['data'], validate=True)
            except (KeyError, TypeError, ValueError) as exc:
                raise RecoveryRefused('Invalid Goal archive chunk data') from exc
            if not part or len(part) > CHUNK_BYTES:
                raise RecoveryRefused('Goal archive chunk exceeds its boundary')
            parts.append(part)
            continue
        if 'payload_ref' not in event:
            yield event
            continue
        reference = event['payload_ref']
        if (event.get('goal_execution_archive') not in GOAL_EXECUTION_LAUNCH_FORMATS
                or not isinstance(reference, dict)
                or set(reference) != {'sha256', 'chunks', 'byte_length'}
                or not isinstance(reference['sha256'], str)
                or type(reference['chunks']) is not int or reference['chunks'] <= 0
                or type(reference['byte_length']) is not int or reference['byte_length'] <= 0):
            raise RecoveryRefused('Invalid Goal archive reference')
        parts = chunks.pop(reference['sha256'], [])
        if len(parts) != reference['chunks']:
            raise RecoveryRefused('Goal archive is incomplete')
        body = b''.join(parts)
        if (len(body) != reference['byte_length']
                or hashlib.sha256(body).hexdigest() != reference['sha256']):
            raise RecoveryRefused('Goal archive digest does not match')
        try:
            decoded = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RecoveryRefused('Goal archive JSON is invalid') from exc
        if not isinstance(decoded, dict) or decoded.get('type') != event.get('type'):
            raise RecoveryRefused('Goal archive event identity changed')
        yield decoded
    # Unreferenced tail chunks are an interrupted write, never an event. The
    # preceding effect admission remains pending and ordinary replay refuses.
