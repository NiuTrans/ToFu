"""Frozen source history must remain exact and bounded before native import."""
import json
import sqlite3
import tracemalloc

import pytest

from lib.storage_sidecar import tofudb_migration_transcript as transcript
from lib.storage_sidecar.archived_message_codec import encode_archived_message_sequence_with_metrics
from lib.storage_sidecar.tofudb_migration_source import MigrationSourceError, SQLiteMigrationSource

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('chunk_size', [1, 3, 17, 256])
def test_array_stream_preserves_unicode_escaping_and_nested_values(chunk_size):
    messages = [{'role': 'user', 'content': '你好🙂 } \\" \n', 'nested': [True, None, {'x': 1}]},
                {'role': 'assistant', 'content': ''}]
    raw = (' \n' + json.dumps(messages, ensure_ascii=False) + '\t').encode()
    assert list(transcript._array_members(raw[i:i+chunk_size] for i in range(0, len(raw), chunk_size))) == messages


@pytest.mark.parametrize('raw', [b'', b'{}', b'[', b'[{}', b'[{},]', b'[{}{}]', b'[1]', b'[{}]{}', b'[{}]\xff', b'[{"x":]', b'[{"x":NaN}]', b'[{"x":Infinity}]', b'[{"x":1e400}]'])
def test_array_stream_rejects_malformed_or_trailing_input(raw):
    with pytest.raises(MigrationSourceError):
        list(transcript._array_members(raw[i:i+1] for i in range(len(raw))))


def test_array_stream_closes_abandoned_source_and_enforces_total_budget(monkeypatch):
    closed = []
    def chunks():
        try:
            yield b'[{},'
            yield b'{}]'
        finally:
            closed.append(True)
    members = transcript._array_members(chunks())
    assert next(members) == {}
    members.close()
    assert closed == [True]
    monkeypatch.setattr(transcript, 'MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES', 5)
    with pytest.raises(MigrationSourceError, match='byte budget'):
        list(transcript._array_members([b'[{}', b',{}]']))


def test_frozen_history_decodes_compressed_messages_and_enforces_owner(tmp_path):
    path = tmp_path / 'source.db'
    messages = [{'role': 'user', 'content': '保留历史' * 32768}, {'role': 'assistant', 'content': '答案'}]
    raw = encode_archived_message_sequence_with_metrics(messages).stored_document
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE storage_conversations(id TEXT,user_id INTEGER,messages_json BLOB,PRIMARY KEY(id,user_id))')
        db.execute('INSERT INTO storage_conversations VALUES(?,?,?)', ('conv', 7, raw))
    with SQLiteMigrationSource(path) as source:
        assert list(transcript.frozen_messages(source, 'conv', 7)) == messages
        with pytest.raises(MigrationSourceError, match='missing'):
            list(transcript.frozen_messages(source, 'conv', 8))


def test_large_outer_transcript_does_not_accumulate_messages(tmp_path):
    path = tmp_path / 'source.db'
    message = {'role': 'assistant', 'content': 'preserved' * 128}
    encoded = json.dumps(message).encode()
    count = 10000
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE storage_conversations(id TEXT,user_id INTEGER,messages_json BLOB,PRIMARY KEY(id,user_id))')
        db.execute('INSERT INTO storage_conversations VALUES(?,?,?)', ('conv', 7, b'[' + b','.join([encoded] * count) + b']'))
    with SQLiteMigrationSource(path) as source:
        tracemalloc.start()
        try:
            seen = 0
            for observed in transcript.frozen_messages(source, 'conv', 7):
                assert observed == message
                seen += 1
            assert seen == count
            assert tracemalloc.get_traced_memory()[1] < 4 * 1024 * 1024
        finally:
            tracemalloc.stop()


@pytest.mark.parametrize('number', ['18446744073709551615', '18446744073709551616',
                                  '-9223372036854775809', '1.25e-20'])
def test_numeric_projection_matches_the_application_json_decoder(number):
    import orjson
    raw = ('[{"value":' + number + '}]').encode()
    assert list(transcript._array_members([raw])) == orjson.loads(raw)


def test_one_large_unicode_message_has_a_bounded_decode_peak():
    raw = json.dumps([{'content': 'x' * (4 * 1024 * 1024) + '🙂'}], ensure_ascii=False).encode()
    tracemalloc.start()
    try:
        items = transcript._array_members(raw[i:i+262144] for i in range(0, len(raw), 262144))
        message = next(items)
        assert len(message['content']) == 4 * 1024 * 1024 + 1
        with pytest.raises(StopIteration):
            next(items)
        # A single astral character promotes both parser text and decoded
        # content to four-byte Python Unicode; allow both copies plus input.
        assert tracemalloc.get_traced_memory()[1] < 10 * len(raw)
    finally:
        tracemalloc.stop()


def test_frozen_page_packing_keeps_whole_messages_and_exact_descriptors(tmp_path, monkeypatch):
    import hashlib
    from lib.storage_sidecar.generated_tofudb_migration import FROZEN_TRANSCRIPTS
    path = tmp_path / 'source.db'
    messages = [{'content': 'a' * 20}, {'content': 'b' * 200}, {'content': 'c' * 20}]
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE storage_conversations(id TEXT,user_id INTEGER,messages_json BLOB,PRIMARY KEY(id,user_id))')
        db.execute('INSERT INTO storage_conversations VALUES(?,?,?)', ('conv', 7, json.dumps(messages).encode()))
    monkeypatch.setitem(FROZEN_TRANSCRIPTS, 'targetPageBytes', 100)
    with SQLiteMigrationSource(path) as source:
        pages = list(transcript.frozen_pages(source, 'conv', 7))
        assert len(pages) == 3
        for index, (raw, descriptor) in enumerate(pages):
            assert json.loads(raw) == [messages[index]]
            assert descriptor == {'start': index, 'count': 1, 'bytes': len(raw),
                                  'sha256': hashlib.sha256(raw).hexdigest()}
        monkeypatch.setitem(FROZEN_TRANSCRIPTS, 'maximumMessages', 2)
        with pytest.raises(MigrationSourceError, match='message budget'):
            list(transcript.frozen_pages(source, 'conv', 7))
