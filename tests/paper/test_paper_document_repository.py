"""Durable paper identity, build publication, owner fences and reading CAS."""
from __future__ import annotations

import copy

import pytest

from lib.paper.builds.document_repository import PaperDocumentRepository
from lib.storage.errors import StorageError
from tests.paper import test_paper_build_retention_faults as storage_support
from tests.paper.test_paper_build_retention import _repository
from tests.paper.test_paper_build_cache import _build

pytestmark = pytest.mark.unit
storage = storage_support.storage
SOURCE = {'source': 'local', 'project_root': '/owned', 'path': 'main.tex',
          'main_file': 'main.tex', 'label': 'Main paper'}


def repository(storage, owner=7):
    return PaperDocumentRepository(owner, client_factory=lambda **_: storage.client)


def test_identity_is_stable_without_source_access_and_is_owner_scoped(storage):
    papers = repository(storage)
    document = papers.register(SOURCE)
    changed = {**SOURCE, 'label': 'Renamed display', 'path': 'chapter.tex'}
    assert papers.register(changed)['document_id'] == document['document_id']
    assert papers.resolve([changed]) == [document['document_id']]
    other = repository(storage, 8)
    assert other.resolve([SOURCE]) == [None]
    with pytest.raises(StorageError) as missing:
        other.get(document['document_id'])
    assert missing.value.code == 'database_not_found'
    assert papers.current('conversation') is None


def test_document_and_conversation_counts_do_not_refuse_durable_work(storage):
    papers = repository(storage)
    identity = papers.register(SOURCE)['document_id']
    for index in range(130):
        source = {**SOURCE, 'path': f'chapter-{index}.tex', 'main_file': f'chapter-{index}.tex'}
        created = papers.register(source)
        assert papers.resolve([source]) == [created['document_id']]
    for index in range(520):
        conversation = f'conversation-{index}'
        papers.request(identity, conversation, f'request-{index}')
        assert papers.current(conversation)['document_id'] == identity


def test_abandoned_generation_cannot_publish_and_same_request_can_restart(storage, tmp_path):
    papers = repository(storage)
    identity = papers.register(SOURCE)['document_id']
    generation = papers.request(identity, 'conversation', 'same-request')['generation']
    papers.abandon(identity, generation)
    late = _build(tmp_path, document=identity, status='failed')
    assert papers.finish(identity, generation, late.receipt, None) == {'current': False, 'published': False}
    next_generation = papers.request(identity, 'conversation', 'same-request')['generation']
    assert next_generation == generation + 1
    assert papers.get(identity)['status'] == 'pending'


def test_reading_final_save_can_overtake_scroll_save_but_not_another_window(storage, tmp_path):
    papers = repository(storage)
    identity = papers.register(SOURCE)['document_id']
    build = _build(tmp_path, document=identity)
    generation = papers.request(identity, 'conversation', 'build')['generation']
    with _repository(storage).prepare(build) as proof:
        papers.finish(identity, generation, build.receipt, proof)
    state = {'build_id': build.receipt['build_id'], 'page': 12, 'fraction': .3,
             'horizontal': .5, 'scale': 1.25, 'fit': False, 'reading_mode': True}
    latest = papers.save_reading(identity, state, 0, writer_id='a' * 32, sequence=2)
    assert papers.save_reading(identity, {**state, 'page': 1}, 0, writer_id='a' * 32, sequence=1) == latest
    with pytest.raises(StorageError):
        papers.save_reading(identity, {**state, 'page': 1}, 0, writer_id='a' * 32, sequence=2)
    other = papers.save_reading(identity, {**state, 'page': 20}, latest['revision'], writer_id='b' * 32)
    with pytest.raises(StorageError):
        papers.save_reading(identity, state, 0, writer_id='a' * 32, sequence=3)
    assert papers.get(identity)['reading'] == other['state']


def test_new_generation_and_first_completion_fence_old_publications(storage, tmp_path):
    papers = repository(storage)
    identity = papers.register(SOURCE)['document_id']
    first = papers.request(identity, 'conversation', 'first')['generation']
    assert papers.request(identity, 'conversation', 'first')['generation'] == first
    second = papers.request(identity, 'conversation', 'second')['generation']
    artifacts = _repository(storage)
    stale, current, duplicate = [_build(tmp_path, document=identity) for _ in range(3)]
    with artifacts.prepare(current) as proof:
        assert papers.finish(identity, second, current.receipt, proof) == {'current': True, 'published': True}
        assert papers.finish(identity, second, current.receipt, proof) == {'current': True, 'published': True}
        changed = copy.deepcopy(current.receipt)
        changed['toolchain'] = 'changed'
        with pytest.raises(StorageError) as conflict:
            papers.finish(identity, second, changed, proof)
        assert conflict.value.code == 'database_conflict'
    for build, generation in [(stale, first), (duplicate, second)]:
        with artifacts.prepare(build) as proof:
            assert papers.finish(identity, generation, build.receipt, proof) == {'current': False, 'published': False}
    assert papers.current('conversation')['receipt'] == current.receipt
    papers.abandon(identity, second)
    assert papers.get(identity)['status'] == 'ready'


def test_publication_rollback_retains_previous_pdf_and_reading_cas(storage, tmp_path):
    papers = repository(storage)
    identity = papers.register(SOURCE)['document_id']
    artifacts = _repository(storage)
    good, candidate = [_build(tmp_path, document=identity) for _ in range(2)]
    generation = papers.request(identity, 'conversation', 'first')['generation']
    with artifacts.prepare(good) as proof:
        papers.finish(identity, generation, good.receipt, proof)
    reading = {'build_id': good.receipt['build_id'], 'page': 12, 'fraction': .45,
               'horizontal': .1, 'scale': 1.5, 'fit': False, 'reading_mode': True}
    with pytest.raises(StorageError) as invalid:
        papers.save_reading(identity, {**reading, 'fraction': float('nan')}, 0)
    assert invalid.value.code == 'database_protocol_error'
    assert papers.save_reading(identity, reading, 0) == {'revision': 1, 'state': reading}
    with pytest.raises(StorageError) as conflict:
        papers.save_reading(identity, {**reading, 'page': 1}, 0)
    assert conflict.value.code == 'database_conflict'
    generation = papers.request(identity, 'conversation', 'next')['generation']
    with artifacts.prepare(candidate) as proof:
        storage.client.fail_commit = 'paper.document.finish'
        with pytest.raises(StorageError, match='before commit'):
            papers.finish(identity, generation, candidate.receipt, proof)
    papers.abandon(identity, generation)
    restored = papers.get(identity)
    assert restored['receipt'] == good.receipt
    assert restored['reading'] == reading and restored['reading_revision'] == 1
    assert restored['status'] == 'interrupted'


def test_unverified_or_failed_attempt_cannot_displace_verified_pdf(storage, tmp_path):
    papers = repository(storage)
    identity = papers.register(SOURCE)['document_id']
    artifacts = _repository(storage)
    good = _build(tmp_path, document=identity)
    generation = papers.request(identity, 'conversation', 'good')['generation']
    with artifacts.prepare(good) as proof:
        papers.finish(identity, generation, good.receipt, proof)
    uncertain = _build(tmp_path, document=identity)
    uncertain.receipt.update(target='overleaf', version_verified=False, source_digest=None)
    generation = papers.request(identity, 'conversation', 'cloud')['generation']
    with artifacts.prepare(uncertain) as proof:
        assert papers.finish(identity, generation, uncertain.receipt, proof) == {'current': True, 'published': False}
    failed = _build(tmp_path, document=identity, status='failed')
    generation = papers.request(identity, 'conversation', 'failed')['generation']
    papers.finish(identity, generation, failed.receipt, None)
    assert papers.get(identity)['status'] == 'failed'
    assert papers.get(identity)['receipt'] == good.receipt
