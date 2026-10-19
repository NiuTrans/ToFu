"""Encrypted logical replay carries retained PDF bytes and their atomic pin."""
from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from lib.storage.client import StorageClient
from lib.paper.builds.document_repository import PaperDocumentRepository
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from lib.storage_authority_sql.logical_outbox import LogicalOutboxPipeline, decode_logical_payload
from lib.storage_authority_sql.logical_replay import BackendReplayTarget, replay_records
from lib.storage_authority_sql.logical_shadow import LogicalCommitShadow
from lib.storage_authority_sql.operation_registry import build_registry
from lib.storage_authority_sql.server import create_server
from tests.storage import test_storage_logical_outbox as outbox_support
from tests.paper.test_paper_build_cache import _build
from tests.paper.test_paper_build_retention import _lease, _pin, _repository

pytestmark = pytest.mark.unit
_isolated_logical_encryption_key = outbox_support._isolated_logical_encryption_key


def test_retained_manuscript_replays_as_complete_bytes_and_workspace_reference(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = outbox_support._config(tmp_path / 'source')
    backend = SQLiteBackend(config)
    backend.start()
    pipeline = LogicalOutboxPipeline.from_config(config, backend)
    pipeline.start()
    server = create_server(backend, outbox_support._TOKEN, logical_outbox=pipeline)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = StorageClient('127.0.0.1', server.server_address[1], outbox_support._TOKEN)
    storage = SimpleNamespace(client=client)
    build = _build(tmp_path)
    lease_id = uuid.uuid4().hex
    marker = 'retained-replay-complete-' + uuid.uuid4().hex
    try:
        with _repository(storage).prepare(build) as proof:
            _pin(storage, build, proof)
        _lease(storage, proof, lease_id=lease_id)
        papers = PaperDocumentRepository(7, client_factory=lambda **_: client)
        source = {'source': 'local', 'project_root': '/owned/replay', 'path': 'main.tex',
                  'main_file': 'main.tex', 'label': 'main.tex'}
        document_id = papers.register(source)['document_id']
        generation = papers.request(document_id, 'chat', 'revision-one')['generation']
        chat_build = _build(tmp_path, document=document_id)
        with _repository(storage).prepare(chat_build) as chat_proof:
            papers.finish(document_id, generation, chat_build.receipt, chat_proof)
        reading = {'build_id': chat_build.receipt['build_id'], 'page': 9, 'fraction': .3,
                   'horizontal': .5, 'scale': 1.5, 'fit': False, 'reading_mode': True}
        papers.save_reading(document_id, reading, 0)
        _lease(storage, chat_proof, lease_id=lease_id)
        client.command('record.put', {'namespace': 'retained-replay-marker', 'key': 'complete',
                                     'value': {'marker': marker}}, uuid.uuid4().hex)
        stream_id = pipeline.status()['stream_id']
        committed = pipeline.status()['captured_total']
        assert committed > 0
        outbox_support._wait_until(lambda: pipeline.status()['published_sequence'] >= committed)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        pipeline.close()
        backend.close()

    with LogicalCommitShadow(config.logical_shadow_dir, stream_id=stream_id) as sink:
        records = sink.read_records()
    assert any(marker in json.dumps(decode_logical_payload(record)) for record in records)

    target_config = outbox_support._config(tmp_path / 'replayed', role='api')
    target_backend = SQLiteBackend(target_config)
    target_backend.start()
    target = BackendReplayTarget(target_backend, target_name='retained-paper-drill', stream_id=stream_id)
    registry = build_registry()
    def query(operation, payload):
        return target_backend.query(operation, lambda session: registry[operation].handler(session, payload),
                                    time.monotonic() + 5)
    try:
        replay_records(records, target)
        checkpoint = target.checkpoint()
        target.apply_and_checkpoint(records[-1], checkpoint)
        assert target.checkpoint() == checkpoint
        workspace = query('research.workspace.get', {'user_id': 7, 'paper_hash': 'paper', 'lang': 'en'})
        assert workspace['workspace']['compilation']['build']['build_id'] == proof['build_id']
        assert query('paper.artifact.latest', {'user_id': 7, 'document_id': proof['document_id']}) == {
            'build_id': proof['build_id']}
        chunk = query('paper.artifact.read', {**proof, 'user_id': 7, 'lease_id': lease_id, 'sequence': 0})
        assert base64.b64decode(chunk['data']) == (build.directory / 'document.pdf').read_bytes()
        assert query('paper.document.resolve', {'user_id': 7, 'sources': [source]}) == [document_id]
        restored = query('paper.document.current', {'user_id': 7, 'conversation_id': 'chat'})
        assert restored['receipt'] == chat_build.receipt
        assert restored['reading'] == reading and restored['reading_revision'] == 1
        chunk = query('paper.artifact.read', {**chat_proof, 'user_id': 7, 'lease_id': lease_id, 'sequence': 0})
        assert base64.b64decode(chunk['data']) == (chat_build.directory / 'document.pdf').read_bytes()
    finally:
        target_backend.close()
