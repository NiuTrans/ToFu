"""Immutable run-scoped indexes for pending receipt sources and latest job states.

ProgressIndexes advances verified chronological receipt deltas with bounded trie
lookups. Sources and jobs occupy distinct namespaces from consumed scientific
sources. Returned roots remain candidates until a workspace CAS publishes them.
No index grants completion, scientific evidence or external-effect replay.
Dependencies: source_index trie, execution_observation parser, program receipt schema.
"""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import json

from jsonschema import Draft202012Validator

from lib.storage.errors import StorageError
from .source_index import ReceiptSourceIndex, empty_reference, normalize_reference, receipt_key, receipt_digest, digest, encoded
from .execution_observation import execution_state_parser, normalize_execution_observation, provider_scope_for_tool, STATE_VALUES

SOURCE_NAMESPACE = 'research.pending_source_nodes.v1'
JOB_NAMESPACE = 'research.pending_job_nodes.v1'
STATE_HASHES = {state: digest({'state': state}) for state in STATE_VALUES}
HASH_STATES = {value: key for key, value in STATE_HASHES.items()}


class PendingIndex(ReceiptSourceIndex):
    def _write(self, node):
        sha = super()._write(node)
        self.cache.pop(sha, None)
        if self._read(sha) != node:
            raise StorageError('database_integrity', 'Research pending index node was not exactly acknowledged')
        return sha


class PendingSources(PendingIndex):
    namespace = SOURCE_NAMESPACE


class PendingJobs(PendingIndex):
    namespace = JOB_NAMESPACE

    def set_state(self, reference, key, state):
        if state not in STATE_HASHES:
            raise ValueError('Invalid Research indexed job state')
        return self._assign(reference, key, STATE_HASHES[state], replace=True)


def empty_progress():
    return {'sources': empty_reference(), 'jobs': empty_reference(),
            'job_counts': {state: 0 for state in sorted(STATE_VALUES)},
            'last_state': 'planned', 'receipts': 0}


@lru_cache(maxsize=1)
def progress_validator():
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / 'research_program_v1.schema.json').read_text())
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/receiptProgressIndex'})


def normalize_progress(value):
    if (not progress_validator().is_valid(value)
            or not isinstance(value, dict) or set(value) != {'sources','jobs','job_counts','last_state','receipts'}
            or type(value['receipts']) is not int or value['receipts'] < 0
            or value['last_state'] not in STATE_VALUES
            or not isinstance(value['job_counts'],dict) or set(value['job_counts']) != STATE_VALUES
            or any(type(count) is not int or count < 0 for count in value['job_counts'].values())):
        raise ValueError('Invalid Research pending index state')
    sources = normalize_reference(value['sources']); jobs = normalize_reference(value['jobs'])
    if (sources['version'] != 1 or jobs['version'] != 1
            or sum(value['job_counts'].values()) != jobs['count']
            or max(sources['count'], jobs['count']) > value['receipts']):
        raise ValueError('Invalid Research pending index counts')
    return deepcopy(value)


class ProgressIndexes:
    def __init__(self, client, *, user_id, paper_hash, lang, run_id, hypothesis_digest, protocol_digest, cache_entries=None):
        # Reuse the receipt-page scope validation rather than creating a second binding rule.
        from .receipt_pages import ReceiptPages
        binding = ReceiptPages(client, user_id=user_id, paper_hash=paper_hash, lang=lang, run_id=run_id,
                               hypothesis_digest=hypothesis_digest, protocol_digest=protocol_digest)
        arguments = dict(user_id=user_id, paper_hash=paper_hash, lang=lang, cache_entries=cache_entries)
        self.sources = PendingSources(client, **arguments)
        self.jobs = PendingJobs(client, **arguments)
        self.sources.prefix = self.jobs.prefix = binding.prefix

    def lookup_source(self, progress, receipt):
        progress = normalize_progress(progress); key = receipt_key(receipt)
        return self.sources.lookup(progress['sources'], key) if key else None

    def advance(self, progress, receipts, bindings=()):
        """Index only verified appended rows; callers must publish roots with their receipt head."""
        from .program import _normalize_tool_receipts
        result = normalize_progress(progress); parse = execution_state_parser(bindings)
        for receipt in receipts:
            canonical = _normalize_tool_receipts([receipt])
            if len(canonical) != 1 or encoded(canonical[0]) != encoded(receipt):
                raise ValueError('Research pending index requires canonical complete receipts')
            key = receipt_key(receipt)
            if key:
                result['sources'] = self.sources.insert(result['sources'], key, receipt_digest(receipt))
            result['receipts'] += 1
            state = parse(receipt)
            if state is None:
                continue
            result['last_state'] = state
            observation = receipt.get('execution_observation')
            if receipt.get('status') != 'done' or not isinstance(observation,dict):
                continue
            observation = normalize_execution_observation(observation)
            if not observation.get('job_id'):
                continue
            scope = observation.get('provider_scope') or provider_scope_for_tool(receipt.get('tool') or '')
            job_key = digest({'scope':scope, 'job_id':observation['job_id']})
            previous = self.jobs.lookup(result['jobs'],job_key)
            if previous is not None:
                if previous not in HASH_STATES:
                    raise StorageError('database_integrity','Research indexed job state is invalid')
                result['job_counts'][HASH_STATES[previous]] -= 1
            result['jobs'] = self.jobs.set_state(result['jobs'],job_key,state)
            result['job_counts'][state] += 1
        return normalize_progress(result)

    def preview_status(self, progress, receipts, bindings=()):
        """Read-only fold of a bounded current window; old jobs use trie lookups."""
        from .program import MAX_TOOL_RECEIPTS, _normalize_tool_receipts
        progress = normalize_progress(progress)
        counts = dict(progress['job_counts']); last = progress['last_state']
        overlay = {}; parse = execution_state_parser(bindings)
        for number, receipt in enumerate(receipts):
            if number >= MAX_TOOL_RECEIPTS:
                raise ValueError('Research indexed current receipt window exceeds workspace capacity')
            canonical = _normalize_tool_receipts([receipt])
            if len(canonical) != 1 or encoded(canonical[0]) != encoded(receipt):
                raise ValueError('Research pending preview requires canonical complete receipts')
            state = parse(receipt)
            if state is None:
                continue
            last = state
            observation = receipt.get('execution_observation')
            if receipt.get('status') != 'done' or not isinstance(observation, dict) or not observation.get('job_id'):
                continue
            scope = observation.get('provider_scope') or provider_scope_for_tool(receipt.get('tool') or '')
            key = digest({'scope':scope,'job_id':observation['job_id']})
            if key in overlay:
                previous = overlay[key]
            else:
                previous_hash = self.jobs.lookup(progress['jobs'],key)
                if previous_hash is not None and previous_hash not in HASH_STATES:
                    raise StorageError('database_integrity','Research indexed job state is invalid')
                previous = HASH_STATES.get(previous_hash)
            if previous is not None:
                counts[previous] -= 1
            overlay[key] = state; counts[state] += 1
        return next((state for state in ('running','queued','planned','failed') if counts[state]),last)

    def status(self, progress):
        progress = normalize_progress(progress)
        return next((state for state in ('running','queued','planned','failed')
                     if progress['job_counts'][state]), progress['last_state'])
