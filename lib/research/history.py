"""Immutable paged observations on the selected storage record authority.

The workspace CAS publishes the archive head; prepared but unpublished pages
never become scientific evidence. Reads/writes carry owner, direction and
language explicitly. Pages are immutable, content addressed, and bounded by
both records and bytes. The current-scope aggregate makes ordinary saves O(1)
in history length; scope changes replay verified pages with bounded memory.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping

from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .decisions import AUDIT_CHECKS
from .observation_audit import AUDIT_VERSION

NAMESPACE = 'research.observations.v1'
PAGE_RUNS = 16
ROLLOVER_RUNS = 24
WORKING_RUNS = 16
CHECKS = AUDIT_CHECKS
OUTCOMES = ('supported', 'refuted', 'inconclusive')
HEX = re.compile(r'^[0-9a-f]{64}$')
MODULUS = 1 << 256


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def scope_digest(program):
    from .program import dependency_digests
    return digest(dependency_digests(program))


def empty_history(program):
    from .source_index import empty_reference
    return {'source_index': empty_reference(), 'head': '', 'count': 0, 'scope': scope_digest(program), 'audit_version': AUDIT_VERSION,
            'outcomes': dict.fromkeys(OUTCOMES, 0), 'evidence_sum': '0' * 64,
            'audit_failures': dict.fromkeys(CHECKS, 0), 'artifact_count': 0}


def normalize_history(value):
    if not isinstance(value, Mapping):
        raise ValueError('Invalid research history descriptor')
    result = copy.deepcopy(dict(value))
    result.setdefault('audit_version', 0)
    from .source_index import empty_reference, normalize_reference
    result['source_index'] = normalize_reference(result.get('source_index', empty_reference(0)))
    keys = {'head', 'count', 'scope', 'outcomes', 'evidence_sum', 'audit_failures', 'artifact_count', 'audit_version', 'source_index'}
    if (type(result['audit_version']) is not int or result['audit_version'] not in range(AUDIT_VERSION + 1)
            or set(result) != keys or type(result['count']) is not int or result['count'] < 0
            or type(result['artifact_count']) is not int or result['artifact_count'] < 0
            or not isinstance(result['head'], str)
            or (result['head'] and not HEX.fullmatch(result['head']))
            or bool(result['head']) != bool(result['count'])
            or any(not isinstance(result[k], str) or not HEX.fullmatch(result[k])
                   for k in ('scope', 'evidence_sum'))):
        raise ValueError('Invalid research history descriptor')
    for name, fields in [('outcomes', OUTCOMES), ('audit_failures', CHECKS)]:
        counts = result[name]
        if (not isinstance(counts, Mapping) or set(counts) != set(fields)
                or any(type(v) is not int or v < 0 for v in counts.values())):
            raise ValueError('Invalid research history aggregate')
    eligible = sum(result['outcomes'].values())
    if (eligible > result['count'] or result['artifact_count'] > eligible
            or (not eligible and result['evidence_sum'] != '0' * 64)
            or any(v > eligible for v in result['audit_failures'].values())):
        raise ValueError('Invalid research history aggregate')
    return result


def summary_for_program(program):
    value = program.get('history')
    if value is None:
        return empty_history(program)
    history = normalize_history(value)
    if history['scope'] != scope_digest(program):
        return empty_history(program)
    if history['audit_version'] != AUDIT_VERSION:
        eligible = sum(history['outcomes'].values())
        history['audit_failures'] = dict.fromkeys(CHECKS, eligible)
        history['outcomes'] = dict.fromkeys(OUTCOMES, 0)
        history['outcomes']['inconclusive'] = eligible
    return history


def observation_commitment(run):
    from .program import normalize_run_evidence
    return int(digest(normalize_run_evidence([run])[0]), 16)


def aggregate_observation(history, run, program):
    from .program import dependency_digests
    from .decisions import authoritative, manuscript_audit, effective_outcome
    dependencies = dependency_digests(program)
    if not all(run.get(key) == value for key, value in dependencies.items()):
        return
    current = dict(run, validity='current')
    if not authoritative(current):
        return
    history['outcomes'][effective_outcome(current, program.get('protocol'))] += 1
    history['evidence_sum'] = f"{(int(history['evidence_sum'], 16) + observation_commitment(run)) % MODULUS:064x}"
    history['artifact_count'] += bool(run.get('artifact_ref') or run.get('artifact_refs'))
    audit = manuscript_audit([current], [], protocol=program.get('protocol'))
    for check in audit['checks']:
        if check['id'] in CHECKS and not check['ok']:
            history['audit_failures'][check['id']] += 1


class ResearchHistoryRepository:
    """A direction-scoped content-addressed archive, published only by workspace CAS."""
    def __init__(self, client, *, user_id, paper_hash, lang):
        owner = require_user_id(user_id, context='research history owner')
        if not isinstance(paper_hash, str) or not re.fullmatch(r'[0-9a-f]{32}', paper_hash):
            raise ValueError('Invalid research direction hash')
        if lang not in {'en', 'zh'}:
            raise ValueError('Invalid research history language')
        self.client = client
        self.prefix = f'{owner}:{paper_hash}:{lang}:'

    def read_chunk(self, head, count):
        from .program import MAX_PROGRAM_BYTES, normalize_run_evidence
        if not isinstance(head, str) or not HEX.fullmatch(head) or type(count) is not int or count <= 0:
            raise StorageError('database_integrity', 'Invalid research history boundary')
        row = self.client.query('record.get', {'namespace': NAMESPACE, 'key': self.prefix + head})
        value = row.get('value') if isinstance(row, Mapping) else None
        if (not isinstance(value, Mapping) or row.get('version') != 1
                or len(encoded(value)) > MAX_PROGRAM_BYTES or digest(value) != head
                or set(value) != {'previous', 'previous_count', 'count', 'runs'}
                or type(value['count']) is not int or value['count'] != count or type(value['previous_count']) is not int
                or not isinstance(value['runs'], list) or not 1 <= len(value['runs']) <= PAGE_RUNS
                or count - value['previous_count'] != len(value['runs'])
                or value['previous_count'] < 0
                or not isinstance(value['previous'], str)
                or bool(value['previous']) != bool(value['previous_count'])
                or (value['previous'] and not HEX.fullmatch(value['previous']))):
            raise StorageError('database_integrity', 'Research history page is missing or invalid')
        try:
            runs = normalize_run_evidence(value['runs'])
        except ValueError as exc:
            raise StorageError('database_integrity', 'Research history observations are invalid') from exc
        if runs != value['runs'] or any(r['execution_status'] != 'completed' for r in runs):
            raise StorageError('database_integrity', 'Research history observations are not canonical completed results')
        return copy.deepcopy(dict(value))

    def iter_chunks(self, history):
        head, count = history['head'], history['count']
        while head:
            chunk = self.read_chunk(head, count)
            yield head, chunk
            head, count = chunk['previous'], chunk['previous_count']

    def prepare(self, prior_history, program, observations):
        """Return a new descriptor; no prepared page is authoritative before CAS."""
        from .program import MAX_PROGRAM_BYTES
        previous = normalize_history(prior_history) if prior_history else empty_history(program)
        if previous['head']:
            self.read_chunk(previous['head'], previous['count'])
        if previous['scope'] != scope_digest(program) or previous['audit_version'] != AUDIT_VERSION:
            history = empty_history(program)
            history.update(head=previous['head'], count=previous['count'], source_index=previous['source_index'])
            for _, chunk in self.iter_chunks(previous):
                for run in chunk['runs']:
                    aggregate_observation(history, run, program)
        else:
            history = copy.deepcopy(previous)
        pending = list(observations)
        while pending:
            batch = []
            while pending and len(batch) < PAGE_RUNS:
                proposed = {'previous': history['head'], 'previous_count': history['count'],
                            'count': history['count'] + len(batch) + 1, 'runs': batch + [pending[0]]}
                if len(encoded(proposed)) > MAX_PROGRAM_BYTES:
                    if not batch:
                        raise ValueError('Research observation exceeds archive page byte budget')
                    break
                batch.append(pending.pop(0))
            chunk = {'previous': history['head'], 'previous_count': history['count'],
                     'count': history['count'] + len(batch), 'runs': batch}
            head = digest(chunk)
            key = self.prefix + head
            try:
                result = self.client.command('record.put', {
                    'namespace': NAMESPACE, 'key': key, 'value': chunk, 'expected_version': 0,
                }, f'research-history:{key}')
                if not isinstance(result, Mapping) or result.get('version') != 1:
                    raise StorageError('database_internal', 'Research history write was not acknowledged')
            except StorageError as exc:
                # An ambiguous ACK or identical preparation may have committed.
                # Only exact immutable bytes satisfy that proof; absence/conflict
                # propagates without accepting a workspace or repeating an effect.
                try:
                    self.read_chunk(head, chunk['count'])
                except StorageError:
                    raise exc
            for run in batch:
                aggregate_observation(history, run, program)
            history.update(head=head, count=chunk['count'])
        return history

    def page(self, history, *, cursor=None):
        history = normalize_history(history)
        if cursor is not None and (type(cursor) is not int or not 0 < cursor <= history['count']):
            raise ValueError('Invalid research history cursor')
        target = history['count'] if cursor is None else cursor
        for head, chunk in self.iter_chunks(history):
            if chunk['count'] == target:
                return {'head': history['head'], 'page': head, 'total': history['count'],
                        'runs': chunk['runs'], 'next_cursor': chunk['previous_count'] or None}
        if target:
            raise ValueError('Research history cursor is not a committed page boundary')
        return {'head': '', 'page': '', 'total': 0, 'runs': [], 'next_cursor': None}


def compact_observations(program):
    """Keep in-flight/cited evidence; archive by record or document pressure."""
    from .program import normalize_run_evidence, normalize_program_fields, MAX_PROGRAM_BYTES
    runs = normalize_run_evidence(program.get('runs'))

    def pressured(rows):
        proposal = dict(program, runs=rows)
        canonical = dict(proposal, **normalize_program_fields(proposal))
        # Reserve the bounded scope aggregate and newly derived claim reasons;
        # this is document headroom, not an execution/concurrency throttle.
        return len(encoded(canonical)) > MAX_PROGRAM_BYTES - 4096

    if len(runs) < ROLLOVER_RUNS and not pressured(runs):
        return runs, []
    refs = {ref for claim in program.get('claims') or [] for ref in claim.get('evidence_refs') or []}
    retained, archived = list(runs), []
    for run in runs:
        if len(retained) <= WORKING_RUNS and not pressured(retained):
            break
        identities = {run['id'], run['task_id'], run['artifact_ref'], *run['artifact_refs']} - {''}
        if run['execution_status'] == 'completed' and not refs.intersection(identities):
            archived.append(run)
            retained.remove(run)
    return retained, archived
