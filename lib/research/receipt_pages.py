"""Immutable bounded receipt pages for unfinished experiment continuity.

ReceiptPages.prepare appends iterable receipts without loading total history;
iter_pages reads newest page first; open_receipts verifies the full chain and
streams original chronology through a scoped disk spool.
The owning workspace CAS must publish the returned reference. Prepared pages
alone grant neither scientific evidence nor tool replay. Dependencies: program
receipt schema, explicit identity and selected record storage authority.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import struct
import tempfile

from jsonschema import Draft202012Validator

from lib.identity import require_user_id
from lib.storage.errors import StorageError

NAMESPACE = 'research.receipt_pages.v1'
PAGE_BYTES = 128 * 1024
PAGE_RECEIPTS = 32


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


@lru_cache(maxsize=2)
def validator(kind):
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                         'research_program_v1.schema.json').read_text())
    return Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/' + kind})


def normalize_reference(value):
    if not validator('receiptPageReference').is_valid(value):
        raise ValueError('Invalid Research receipt page reference')
    # jsonschema treats integral floats as integers; persisted counts are exact ints.
    if type(value['count']) is not int:
        raise ValueError('Invalid Research receipt page count')
    return deepcopy(value)


def empty_reference():
    return {'head': '', 'count': 0}


class ReceiptPages:
    def __init__(self, client, *, user_id, paper_hash, lang, run_id,
                 hypothesis_digest, protocol_digest):
        owner = require_user_id(user_id, context='research receipt page owner')
        if (not isinstance(paper_hash, str) or not re.fullmatch('[0-9a-f]{32}', paper_hash)
                or lang not in ('en', 'zh') or not isinstance(run_id, str)
                or not run_id or len(run_id) > 96
                or any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
                       for value in (hypothesis_digest, protocol_digest))):
            raise ValueError('Invalid Research receipt page binding')
        scope = digest({'run_id': run_id, 'hypothesis_digest': hypothesis_digest,
                        'protocol_digest': protocol_digest})
        self.client = client
        self.prefix = f'{owner}:{paper_hash}:{lang}:{scope}:'

    def read_page(self, reference):
        reference = normalize_reference(reference)
        if not reference['head']:
            raise ValueError('Empty Research receipt history has no page')
        row = self.client.query('record.get', {'namespace': NAMESPACE,
                               'key': self.prefix + reference['head']})
        value = row.get('value') if isinstance(row, Mapping) else None
        try:
            valid = (isinstance(value, dict) and type(row.get('version')) is int
                     and row['version'] == 1 and validator('receiptPage').is_valid(value)
                     and len(encoded(value)) <= PAGE_BYTES and digest(value) == reference['head']
                     and type(value['count']) is int and value['count'] == reference['count'])
            if not valid:
                raise ValueError('invalid receipt page')
            previous = normalize_reference(value['previous'])
            if previous['count'] + len(value['receipts']) != value['count']:
                raise ValueError('invalid receipt page lineage')
            from .program import _normalize_tool_receipts
            if _normalize_tool_receipts(value['receipts']) != value['receipts']:
                raise ValueError('noncanonical receipt page')
        except (ValueError, TypeError, RecursionError) as exc:
            raise StorageError('database_integrity', 'Research receipt page is missing or invalid') from exc
        return deepcopy(value)

    def iter_pages(self, reference, *, base=None):
        reference = normalize_reference(reference)
        base = normalize_reference(empty_reference() if base is None else base)
        while reference != base:
            if not reference['head'] or reference['count'] <= base['count']:
                raise StorageError('database_integrity', 'Research receipt delta does not reach its published base')
            page = self.read_page(reference)
            yield page
            reference = page['previous']

    @contextmanager
    def open_receipts(self, reference, inline=(), *, base=None):
        """Verify the entire chain, then stream chronological receipts with bounded RAM.

        A private disk spool reverses the linked pages. No partial history reaches
        a consumer before every ancestor is verified. The context owns cleanup,
        including early consumer exit, exceptions and cancellation.
        """
        from .program import _normalize_tool_receipts
        from .program import MAX_TOOL_RECEIPTS
        tail = []
        for receipt in inline:
            if len(tail) == MAX_TOOL_RECEIPTS:
                raise ValueError('Research receipt stream inline tail exceeds workspace capacity')
            tail.append(receipt)
        canonical = _normalize_tool_receipts(tail)
        if encoded(canonical) != encoded(tail):
            raise ValueError('Research receipt stream requires canonical complete inline receipts')
        with tempfile.TemporaryFile(mode='w+b') as spool:
            for page in self.iter_pages(reference, base=base):
                payload = encoded(page['receipts'])
                spool.write(payload)
                spool.write(struct.pack('>I', len(payload)))
            end = spool.tell()

            def chronological():
                position = end
                while position:
                    spool.seek(position - 4)
                    size = struct.unpack('>I', spool.read(4))[0]
                    position -= size + 4
                    spool.seek(position)
                    for receipt in json.loads(spool.read(size)):
                        yield receipt
                for receipt in canonical:
                    yield deepcopy(receipt)

            receipts = chronological()
            try:
                yield receipts
            finally:
                receipts.close()

    def _write(self, previous, receipts):
        page = {'previous': deepcopy(previous), 'count': previous['count'] + len(receipts),
                'receipts': deepcopy(receipts)}
        reference = {'head': digest(page), 'count': page['count']}
        key = self.prefix + reference['head']
        try:
            result = self.client.command('record.put', {'namespace': NAMESPACE, 'key': key,
                'value': page, 'expected_version': 0}, f'research-receipt-page:{key}')
            if not isinstance(result, Mapping) or type(result.get('version')) is not int or result['version'] != 1:
                raise StorageError('database_internal', 'Research receipt page write was not acknowledged')
        except StorageError as exc:
            try:
                self.read_page(reference)
            except StorageError:
                raise exc
        # Publication requires exact immutable readback even after a success-shaped ACK.
        self.read_page(reference)
        return reference

    def prepare(self, previous, receipts):
        """Prepare immutable pages; return a candidate pointer without publishing it."""
        previous = normalize_reference(previous)
        if previous['head']:
            self.read_page(previous)
        from .program import _normalize_tool_receipts
        batch = []
        for receipt in receipts:
            canonical = _normalize_tool_receipts([receipt])
            if len(canonical) != 1 or canonical[0] != receipt:
                raise ValueError('Research receipt pages require canonical complete receipts')
            proposed = {'previous': previous, 'count': previous['count'] + len(batch) + 1,
                        'receipts': batch + [canonical[0]]}
            if batch and (len(batch) == PAGE_RECEIPTS or len(encoded(proposed)) > PAGE_BYTES):
                previous = self._write(previous, batch)
                batch = []
            single = {'previous': previous, 'count': previous['count'] + 1, 'receipts': [canonical[0]]}
            if len(encoded(single)) > PAGE_BYTES:
                raise ValueError('Research receipt exceeds page byte budget')
            batch.append(canonical[0])
        if batch:
            previous = self._write(previous, batch)
        return previous


class ReceiptPagePublication:
    """Internal preparation port; an HTTP pointer never substitutes for this result."""
    def __init__(self, *, binding, revision, source_digest, previous, reference, prefix_count):
        self.binding = deepcopy(binding)
        self.revision = revision
        self.source_digest = source_digest
        self.previous = deepcopy(previous)
        self.reference = deepcopy(reference)
        self.prefix_count = prefix_count

    def for_workspace(self, candidate, prior, *, client, user_id, paper_hash, lang, revision):
        from .program import normalize_run_evidence, dependency_digests
        if type(self.prefix_count) is not int or not 1 <= self.prefix_count <= 64:
            raise ValueError('Invalid Research receipt publication prefix')
        run_id = self.binding['run_id']
        if (self.binding['user_id'], self.binding['paper_hash'], self.binding['lang'], self.revision) != (
                user_id, paper_hash, lang, revision):
            raise ValueError('Research receipt publication binding or revision changed')
        runs = normalize_run_evidence(candidate.get('runs'))
        source = next((run for run in runs if run['id'] == run_id), None)
        if source is None or digest(source) != self.source_digest:
            raise ValueError('Research receipt publication source changed')
        old = next((run for run in prior.get('runs', []) if run['id'] == run_id), None)
        base = old.get('receipt_history', empty_reference()) if old else empty_reference()
        if self.previous != base:
            raise ValueError('Research receipt publication cannot replace published history')
        dependencies = dependency_digests(candidate)
        if any(source.get(key) != value for key, value in dependencies.items()):
            raise ValueError('Research receipt publication requires the frozen scientific basis')
        if old and (source['tool_receipts'][:len(old['tool_receipts'])] != old['tool_receipts']
                    or any(source.get(key) != old.get(key) for key in dependencies)):
            raise ValueError('Research receipt publication cannot rewrite pending observations')
        repo = ReceiptPages(client, **self.binding)
        page_reference = normalize_reference(self.reference)
        if page_reference['count'] != base['count'] + self.prefix_count:
            raise ValueError('Research receipt publication count changed')
        # Verify exactly the newly prepared prefix and its ancestry to the prior
        # published root; other orphan pages cannot be substituted as evidence.
        pages = []
        while page_reference != base:
            if page_reference['count'] <= base['count']:
                raise ValueError('Research receipt publication lineage changed')
            page = repo.read_page(page_reference)
            pages.append(page)
            page_reference = page['previous']
        receipts = [receipt for page in reversed(pages) for receipt in page['receipts']]
        if receipts != source['tool_receipts'][:self.prefix_count]:
            raise ValueError('Research receipt publication does not match complete source facts')
        source['tool_receipts'] = source['tool_receipts'][self.prefix_count:]
        source['receipt_history'] = deepcopy(self.reference)
        return runs


def prepare_publication(client, workspace, *, user_id, paper_hash, lang, run_id, revision,
                        keep_inline=32):
    """Fold a canonical pending prefix; publication happens only through workspace CAS."""
    from .program import normalize_run_evidence
    if type(revision) is not int or revision < 0 or type(keep_inline) is not int or not 0 <= keep_inline <= 32:
        raise ValueError('Invalid Research receipt publication revision or inline capacity')
    source = next((run for run in normalize_run_evidence(workspace.get('runs')) if run['id'] == run_id), None)
    if source is None or source['execution_status'] not in {'planned', 'queued', 'running'}:
        raise ValueError('Research receipt publication requires an unfinished run')
    prefix_count = len(source['tool_receipts']) - keep_inline
    if prefix_count <= 0:
        raise ValueError('Research receipt publication requires a nonempty prefix')
    binding = dict(user_id=user_id, paper_hash=paper_hash, lang=lang, run_id=run_id,
                   hypothesis_digest=source['hypothesis_digest'], protocol_digest=source['protocol_digest'])
    previous = source.get('receipt_history', empty_reference())
    reference = ReceiptPages(client, **binding).prepare(previous, source['tool_receipts'][:prefix_count])
    return ReceiptPagePublication(binding=binding, revision=revision, source_digest=digest(source),
        previous=previous, reference=reference, prefix_count=prefix_count)


def receipt_reader(client, *, user_id, paper_hash, lang):
    """Create a run-bound complete reader for selected-authority source accounting."""
    def open_run(run):
        return ReceiptPages(client, user_id=user_id, paper_hash=paper_hash, lang=lang,
            run_id=run['id'], hypothesis_digest=run['hypothesis_digest'],
            protocol_digest=run['protocol_digest']).open_receipts(run['receipt_history'], run['tool_receipts'])
    return open_run
