"""Owner-bound persistent source-consumption index published by workspace CAS.

ReceiptSourceIndex is a content-addressed hexadecimal trie: lookup uses at most
64 key digits, independently of history length. Prepared nodes are not evidence
until the workspace publishes their root. The index stores receipt hashes, never
original output; originals remain in observation/history authorities.
"""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from lib.identity import require_user_id
from lib.storage.errors import StorageError

NAMESPACE = 'research.receipt_source_nodes.v1'
HEX = re.compile(r'^[0-9a-f]{64}$')
DIGITS = '0123456789abcdef'


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def empty_reference(version=1):
    return {'version': version, 'root': '', 'count': 0}


def normalize_reference(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'root', 'count'}
            or type(value['version']) is not int or value['version'] not in (0, 1)
            or type(value['count']) is not int or value['count'] < 0
            or not isinstance(value['root'], str) or (value['root'] and not HEX.fullmatch(value['root']))
            or bool(value['root']) != bool(value['count'])
            or (value['version'] == 0 and value['count'])):
        raise ValueError('Invalid Research receipt source index')
    return dict(value)


def receipt_key(row):
    task, call = row.get('source_task_id'), row.get('call_id')
    return digest([task, call]) if isinstance(task, str) and task and isinstance(call, str) and call else None


def receipt_digest(row):
    return digest({key: value for key, value in row.items() if key != 'observed_at'})


@lru_cache(maxsize=1)
def _node_validator():
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / 'research_program_v1.schema.json').read_text())
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/receiptSourceNode'})


class ReceiptSourceIndex:
    namespace = NAMESPACE

    def __init__(self, client, *, user_id, paper_hash, lang, cache_entries=None):
        owner = require_user_id(user_id, context='research source index owner')
        if not isinstance(paper_hash, str) or not re.fullmatch('[0-9a-f]{32}', paper_hash) or lang not in ('en', 'zh'):
            raise ValueError('Invalid Research source index binding')
        self.client = client
        self.prefix = f'{owner}:{paper_hash}:{lang}:'
        if cache_entries is None:
            cache_entries = 65  # one complete trie path remains available without a probe
            try:
                from runtime_guards import probe_system_resources
                probe = probe_system_resources()  # cached launch probe, not another host scan
                memory = probe.effective_memory_available_mb
                cpu = probe.effective_cpu_count
                if type(memory) is int and memory >= 0 and type(cpu) is int and cpu > 0:
                    cache_entries = max(65, memory * 256 // cpu // 64)
            except Exception:
                # Optional capacity discovery must not prevent durable source publication.
                pass
        if type(cache_entries) is not int or cache_entries < 1:
            raise ValueError('Invalid Research source index cache capacity')
        self.capacity = cache_entries
        self.cache = OrderedDict()

    def _cache(self, sha, node):
        self.cache[sha] = node
        self.cache.move_to_end(sha)
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def _read(self, sha):
        if sha in self.cache:
            self.cache.move_to_end(sha)
            return self.cache[sha]
        row = self.client.query('record.get', {'namespace': self.namespace, 'key': self.prefix + sha})
        node = row.get('value') if isinstance(row, dict) else None
        try:
            valid = (isinstance(node, dict) and type(row.get('version')) is int
                     and row['version'] == 1 and digest(node) == sha)
        except (ValueError, TypeError, RecursionError):
            valid = False
        valid = valid and _node_validator().is_valid(node)
        if not valid:
            raise StorageError('database_integrity', 'Research receipt source node is missing or invalid')
        self._cache(sha, node)
        return node

    def _write(self, node):
        sha = digest(node)
        try:
            ack = self.client.command('record.put', {'namespace': self.namespace, 'key': self.prefix + sha,
                'value': node, 'expected_version': 0}, 'research-source-node:' + self.prefix + sha)
            if not isinstance(ack, dict) or ack.get('version') != 1:
                raise StorageError('database_internal', 'Research source node write was not acknowledged')
        except StorageError as exc:
            try:
                if self._read(sha) != node:
                    raise exc
            except StorageError:
                raise exc
        self._cache(sha, deepcopy(node))
        return sha

    def lookup(self, reference, key):
        if not isinstance(key, str) or not HEX.fullmatch(key):
            raise ValueError('Invalid Research source key')
        reference = normalize_reference(reference)
        sha = reference['root']
        for depth in range(65):
            if not sha:
                return None
            node = self._read(sha)
            if node['kind'] == 'leaf':
                if not node['key'].startswith(key[:depth]):
                    raise StorageError('database_integrity', 'Research source trie path is invalid')
                return node['facts'] if node['key'] == key else None
            if depth == 64:
                raise StorageError('database_integrity', 'Research source trie exceeds key depth')
            sha = node['children'].get(key[depth], '')

    def insert(self, reference, key, facts):
        return self._assign(reference, key, facts, replace=False)

    def _assign(self, reference, key, facts, *, replace):
        """Path-copy assignment; consumed-source insertion never enables replacement."""
        if any(not isinstance(value, str) or not HEX.fullmatch(value) for value in (key, facts)):
            raise ValueError('Invalid Research source fact')
        reference = normalize_reference(reference)
        def add(sha, depth):
            if not sha:
                return self._write({'kind': 'leaf', 'key': key, 'facts': facts}), True, True
            node = self._read(sha)
            if node['kind'] == 'leaf':
                if not node['key'].startswith(key[:depth]):
                    raise StorageError('database_integrity', 'Research source trie path is invalid')
                if node['key'] == key:
                    if node['facts'] != facts:
                        if replace:
                            return self._write({'kind': 'leaf', 'key': key, 'facts': facts}), False, True
                        raise ValueError('Research receipt source identity conflicts with retained observation')
                    return sha, False, False
                split = depth
                while node['key'][split] == key[split]:
                    split += 1
                fresh = self._write({'kind': 'leaf', 'key': key, 'facts': facts})
                branch = self._write({'kind': 'branch', 'children': {node['key'][split]: sha, key[split]: fresh}})
                for position in range(split - 1, depth - 1, -1):
                    branch = self._write({'kind': 'branch', 'children': {key[position]: branch}})
                return branch, True, True
            if depth == 64:
                raise StorageError('database_integrity', 'Research source trie exceeds key depth')
            child, added, changed = add(node['children'].get(key[depth], ''), depth + 1)
            if not changed:
                return sha, False, False
            children = dict(node['children']); children[key[depth]] = child
            return self._write({'kind': 'branch', 'children': children}), added, True
        root, added, _ = add(reference['root'], 0)
        return {'version': 1, 'root': root, 'count': reference['count'] + int(added)}

    def replay(self, reference, receipts):
        all_consumed = True
        seen = False
        for row in receipts:
            seen = True
            key = receipt_key(row)
            found = self.lookup(reference, key) if key else None
            if found is None:
                all_consumed = False
            elif found != receipt_digest(row):
                raise ValueError('Research receipt source identity conflicts with retained observation')
        return seen and all_consumed


@contextmanager
def open_receipt_sources(run, receipt_reader=None):
    """Every paged source requires a complete verified reader, including its inline tail."""
    if run.get('receipt_history', {}).get('head'):
        if receipt_reader is None:
            raise ValueError('Research paged source accounting requires a complete receipt reader')
        with receipt_reader(run) as receipts:
            yield receipts
    else:
        yield iter(run.get('tool_receipts', []))


def prepare_source_index(index, reference, history_repository, prior_history, candidate_runs, prior_runs,
                         receipt_reader=None):
    """Return an unpublished root; backfill existing observations once."""
    reference = normalize_reference(reference)
    existing = {run['id'] for run in prior_runs if run['execution_status'] == 'completed'}
    if reference['version'] == 0:
        reference = empty_reference()
        def backfill(run):
            nonlocal reference
            with open_receipt_sources(run, receipt_reader) as receipts:
                for row in receipts:
                    key = receipt_key(row)
                    if key:
                        reference = index.insert(reference, key, receipt_digest(row))
        if prior_history and prior_history['head']:
            for _, page in history_repository.iter_chunks(prior_history):
                for run in page['runs']:
                    backfill(run)
        for run in prior_runs:
            if run['execution_status'] == 'completed':
                backfill(run)
    for run in candidate_runs:
        if run['execution_status'] != 'completed' or run['id'] in existing:
            continue
        before = reference
        seen = False
        all_consumed = True
        with open_receipt_sources(run, receipt_reader) as receipts:
            for row in receipts:
                seen = True
                key = receipt_key(row)
                facts = receipt_digest(row) if key else None
                found = index.lookup(before, key) if key else None
                if found is None:
                    all_consumed = False
                elif found != facts:
                    raise ValueError('Research receipt source identity conflicts with retained observation')
                if key:
                    reference = index.insert(reference, key, facts)
        if seen and all_consumed:
            raise ValueError('Research completed observation contains already consumed receipt sources')
    return reference


def legacy_archive_replay(repository, history, receipts, receipt_reader=None):
    """Verify legacy archive sources with bounded matching state, then migrate."""
    wanted = {}
    seen = False
    unidentified = False
    for row in receipts:
        seen = True
        key = receipt_key(row)
        if key:
            facts = receipt_digest(row)
            if key in wanted and wanted[key] != facts:
                raise ValueError('Research receipt source identity conflicts with retained observation')
            wanted[key] = facts
        else:
            unidentified = True
    found = set()
    for _, page in repository.iter_chunks(history):
        for run in page['runs']:
            with open_receipt_sources(run, receipt_reader) as historical:
                for row in historical:
                    key = receipt_key(row)
                    if key in wanted:
                        if wanted[key] != receipt_digest(row):
                            raise ValueError('Research receipt source identity conflicts with retained observation')
                        found.add(key)
    return seen and not unidentified and all(key in found for key in wanted)
