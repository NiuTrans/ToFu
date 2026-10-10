"""Receipt-head-bound pending indexes published only through workspace CAS.

ProgressCheckpointRepository reads immutable checkpoints tied to owner/run/basis
and an exact receipt page boundary. prepare bootstraps once, then verifies/indexes
only the delta reaching the previously published boundary. Root/node readback
precedes candidate publication. No checkpoint grants science, completion or replay.
Dependencies: receipt_pages, progress_index and the Research program schema.
"""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from lib.storage.errors import StorageError
from .receipt_pages import ReceiptPages, digest, empty_reference, normalize_reference
from .progress_index import ProgressIndexes, empty_progress, normalize_progress

NAMESPACE = 'research.receipt_progress.v1'


@lru_cache(maxsize=1)
def validator():
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / 'research_program_v1.schema.json').read_text())
    return Draft202012Validator({'$defs':schema['$defs'],'$ref':'#/$defs/receiptProgressCheckpoint'})


class ProgressCheckpointRepository:
    def __init__(self, client, *, user_id, paper_hash, lang, run):
        args = dict(user_id=user_id,paper_hash=paper_hash,lang=lang,run_id=run['id'],
                    hypothesis_digest=run['hypothesis_digest'],protocol_digest=run['protocol_digest'])
        self.client = client
        self.pages = ReceiptPages(client, **args)
        self.indexes = ProgressIndexes(client, **args)
        self.prefix = self.pages.prefix

    def _read(self, pointer):
        row = self.client.query('record.get',{'namespace':NAMESPACE,'key':self.prefix+pointer})
        value = row.get('value') if isinstance(row,dict) else None
        try:
            if (not isinstance(value,dict) or type(row.get('version')) is not int or row['version'] != 1
                    or not validator().is_valid(value) or type(value['version']) is not int or digest(value) != pointer):
                raise ValueError('invalid checkpoint')
            history = normalize_reference(value['receipt_history']); state = normalize_progress(value['index'])
            if history['count'] != state['receipts']:
                raise ValueError('checkpoint boundary count mismatch')
            for index, reference in ((self.indexes.sources,state['sources']),(self.indexes.jobs,state['jobs'])):
                if reference['root']:
                    index._read(reference['root'])
        except (ValueError,TypeError) as exc:
            raise StorageError('database_integrity','Research receipt progress checkpoint is missing or invalid') from exc
        return deepcopy(value)

    def read(self, run):
        pointer = run.get('receipt_progress')
        if pointer is None:
            return None
        value = self._read(pointer)
        if value['receipt_history'] != run.get('receipt_history'):
            raise StorageError('database_integrity','Research receipt progress does not match published receipt head')
        return value

    def prepare(self, prior, candidate, bindings=()):
        old = self.read(prior) if prior else None
        boundary = old['receipt_history'] if old else empty_reference()
        history = candidate['receipt_history']
        if old and history == boundary:
            return prior['receipt_progress']
        with self.pages.open_receipts(history, base=boundary) as receipts:
            state = self.indexes.advance(old['index'] if old else empty_progress(),receipts,bindings)
        value = {'version':1,'receipt_history':deepcopy(history),'index':state}
        if state['receipts'] != history['count']:
            raise StorageError('database_integrity','Research receipt progress delta count is invalid')
        pointer = digest(value); key = self.prefix+pointer
        try:
            result = self.client.command('record.put',{'namespace':NAMESPACE,'key':key,'expected_version':0,'value':value},
                                        'research-receipt-progress:'+key)
            if not isinstance(result,dict) or type(result.get('version')) is not int or result['version'] != 1:
                raise StorageError('database_internal','Research receipt progress write was not acknowledged')
        except StorageError as exc:
            try:
                if self._read(pointer) != value: raise exc
            except StorageError:
                raise exc
        if self._read(pointer) != value:
            raise StorageError('database_integrity','Research receipt progress was not exactly acknowledged')
        return pointer
