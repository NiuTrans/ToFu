"""The conversation inspector stays readable while the conversation is written.

``LiveConversation`` serves the v4 diagnostic wire with the native authority's
fences (packages/tofu-db/src/conversation_diagnostic.rs): Turn/chunk reads
require the current conversation revision, a source token binds one Turn's
source generation rather than the conversation revision, and a streaming text
tail refuses its Turn at an unchanged revision. Its ``writer`` commits writes
between the inspector's requests, deterministically, the way a running agent
or swarm does.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from debug import inspect_conversation as inspector
from tests.diagnostics.test_inspect_conversation_api import api_server

pytestmark = pytest.mark.unit

CONFLICT = {'status': 409, 'code': 'conflict', 'detail': 'database_conflict'}
CHUNK = 65536


class LiveConversation:
    def __init__(self, contents, *, revision=7):
        self.revision = revision
        self.turns = []
        for content in contents:
            self._add(content)
        self.history = {revision: self.contents()}
        self.served = Counter()
        self.requests = []
        self.writer = None

    def _add(self, content):
        index = len(self.turns)
        self.turns.append({'turnId': f'turn-{index}', 'ordinal': 2 * index + 1,
                           'projectionRevision': 1, 'content': content, 'tail': False})

    def contents(self):
        return [turn['content'] for turn in self.turns]

    def write(self, index=None, content=None, *, append=None):
        """Commit one write; every committed write advances the revision."""
        if index is not None:
            turn = self.turns[index]
            turn['content'] = content
            turn['projectionRevision'] += 1
        if append is not None:
            self._add(append)
        self.revision += 1
        self.history[self.revision] = self.contents()

    def source(self, index):
        turn = self.turns[index]
        raw = json.dumps({'content': turn['content']}, separators=(',', ':')).encode()
        identity = json.dumps([turn['turnId'], turn['ordinal'], turn['projectionRevision']])
        return raw, hashlib.sha256(identity.encode() + raw).hexdigest()

    def serve(self, request):
        path = urlsplit(request.path)
        if path.path.endswith('/compactions'):
            self.requests.append(('compactions', None, self.revision))
            return 200, {'compactions': [], 'count': 0}
        args = parse_qs(path.query)
        kind = args['kind'][0]
        index = int(args['index'][0]) if 'index' in args else None
        if self.writer is not None:
            self.writer(self, kind, index, args)
        self.requests.append((kind, index, self.revision))
        if kind == 'header':
            return 200, {'revision': self.revision, 'totalCount': len(self.turns), 'turnNative': True,
                         'document': {'metadata': {'id': 'conversation', 'rev': self.revision,
                                                   'title': 'live document', 'settings': {},
                                                   'msg_count': len(self.turns)}, 'messages': []}}
        if args['revision'] != [str(self.revision)]:
            return 409, CONFLICT
        if index >= len(self.turns):
            return 400, {'status': 400, 'code': 'invalid_request'}
        turn = self.turns[index]
        if turn['tail']:
            return 409, CONFLICT
        raw, token = self.source(index)
        if kind == 'turn':
            return 200, {'revision': self.revision, 'totalCount': len(self.turns), 'index': index,
                         'turn': {'turnId': turn['turnId'], 'conversationId': 'conversation',
                                  'laneId': 'main', 'actor': 'agent', 'ordinal': turn['ordinal'],
                                  'projectionRevision': turn['projectionRevision']},
                         'source': {'format': 'projection', 'totalBytes': len(raw), 'token': token},
                         'patches': []}
        if args['source_token'] != [token]:
            return 409, CONFLICT
        offset, limit = int(args['offset'][0]), int(args['limit'][0])
        data = raw[offset:offset + limit]
        self.served[index] += len(data)
        end = offset + len(data)
        return 200, {'revision': self.revision, 'index': index, 'offset': offset,
                     'totalBytes': len(raw), 'sourceToken': token,
                     'nextOffset': end if end < len(raw) else None,
                     'data': base64.b64encode(data).decode()}


def every(period, write):
    """Commit ``write`` before every ``period``-th diagnostic request, forever."""
    seen = 0

    def writer(conversation, kind, index, args):
        nonlocal seen
        seen += 1
        if seen % period == 0:
            write(conversation, kind, index)
    return writer


def body(index, size=3 * CHUNK):
    return f'turn {index} ' + chr(0x4e00 + index) * (size // 3)


@pytest.fixture(autouse=True)
def _no_retry_pauses(monkeypatch):
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))


def _inspect(conversation, capsys, *extra):
    with api_server(conversation.serve) as url:
        code = inspector.main(['conversation', '--api-url', url, '--no-logs', *extra])
    return code, capsys.readouterr()


def _read(conversation, **options):
    with api_server(conversation.serve) as url:
        document, _ = inspector._load_api_document(
            inspector._DiagnosticAPI(url, None), 'conversation', full=True, **options)
    return document


def test_writes_interleaved_between_reads_still_yield_one_verified_revision(capsys):
    """Reproduction: an unrelated write lands before every 9th request, forever.

    A whole-conversation revision fence needs ~16 quiet requests and so can
    never finish. Fencing each read on its Turn's source token lets settled
    turns survive revision moves; a short re-verification window then proves
    one revision without re-downloading them.
    """
    conversation = LiveConversation([body(index) for index in range(3)])
    conversation.writer = every(9, lambda conversation, *_: conversation.write())
    code, output = _inspect(conversation, capsys)
    assert code == 0, output.err
    final = conversation.revision
    assert f'coverage: 3 read, 0 unread; revision {final};' in output.out
    assert f'rev:        {final}' in output.out
    assert 'BEST-EFFORT' not in output.out and not output.err
    for index in range(3):
        assert body(index)[:40] in output.out
        size = len(conversation.source(index)[0])
        # Each source is downloaded once; later passes add one-byte witnesses.
        assert size <= conversation.served[index] <= size + inspector.SNAPSHOT_PASSES


def test_running_final_turn_rewritten_between_reads_reports_its_snapshot_content():
    conversation = LiveConversation([body(index) for index in range(3)])
    rewrites = iter(range(2))

    def rewrite_final(conversation, kind, index, args):
        if kind == 'chunk' and index == 2 and args['offset'] != ['0'] and next(rewrites, None) is not None:
            conversation.write(2, body(2) + f' revision {conversation.revision + 1}')
    conversation.writer = rewrite_final
    document = _read(conversation)
    final = conversation.revision
    assert document['diagnostic_snapshot'] == 'consistent'
    assert document['diagnostic_revision'] == final
    assert [message['content'] for message in document['messages']] == conversation.history[final]
    descriptors = Counter(index for kind, index, _ in conversation.requests if kind == 'turn')
    # Settled turns are read once and later only witnessed; the final Turn is re-read.
    assert descriptors[0] == descriptors[1] == 1 and descriptors[2] >= 2
    for index in (0, 1):
        assert conversation.served[index] <= len(conversation.source(index)[0]) + inspector.SNAPSHOT_PASSES
    assert conversation.served[2] > len(conversation.source(2)[0])


def test_turn_appended_during_read_extends_the_verified_snapshot():
    conversation = LiveConversation([body(index) for index in range(3)])
    appended = False

    def append(conversation, kind, index, args):
        nonlocal appended
        if kind == 'chunk' and index == 1 and not appended:
            appended = True
            conversation.write(append='queued follow-up')
    conversation.writer = append
    document = _read(conversation)
    assert document['diagnostic_snapshot'] == 'consistent'
    assert document['diagnostic_total_count'] == 4
    assert document['diagnostic_message_indices'] == [0, 1, 2, 3]
    assert [message['content'] for message in document['messages']] == conversation.history[conversation.revision]


def test_writes_that_never_pause_give_a_labelled_best_effort_view(capsys):
    """A write before every 3rd request: single reads succeed, no window ever closes."""
    conversation = LiveConversation([body(index) for index in range(3)])
    conversation.writer = every(3, lambda conversation, *_: conversation.write())
    code, output = _inspect(conversation, capsys)
    assert code == 3, output.err
    assert 'BEST-EFFORT' in output.out
    assert 'not one snapshot' in output.out
    assert '; revision ' not in output.out.split('coverage:', 1)[1].split('\n', 1)[0]
    assert '[as of revision ' in output.out
    for index in range(3):
        assert body(index)[:40] in output.out


def test_best_effort_view_never_mixes_a_changed_turn_into_its_labelled_revision():
    """Each shown turn is exactly its content at the revision it is labelled with."""
    conversation = LiveConversation([body(index, 600) for index in range(3)])
    count = 0

    def churn(conversation, kind, index, args):
        nonlocal count
        count += 1
        if count % 3 == 0:
            target = count // 3 % 3
            conversation.write(target, body(target, 600) + f' @{conversation.revision + 1}')
    conversation.writer = churn
    document = _read(conversation)
    assert document['diagnostic_snapshot'] == 'best-effort'
    assert len(document['messages']) >= 2
    assert len(set(document['diagnostic_turn_revisions'])) > 1
    low, high = document['diagnostic_revision_bounds']
    assert low == min(document['diagnostic_turn_revisions']) and high == document['diagnostic_revision']
    for index, revision, message in zip(document['diagnostic_message_indices'],
                                        document['diagnostic_turn_revisions'],
                                        document['messages']):
        assert message['content'] == conversation.history[revision][index]


def test_final_turn_that_changes_on_every_read_is_live_with_its_last_complete_read(capsys):
    conversation = LiveConversation([body(0, 600), body(1, 600), body(2)])
    size = len(conversation.source(2)[0])
    first_read_done = False
    interrupted = set()

    def stream_final(conversation, kind, index, args):
        # After one complete read, the running final Turn gets a structural
        # write, then one in the middle of every later download of it.
        nonlocal first_read_done
        if not first_read_done:
            if conversation.served[2] >= size:
                first_read_done = True
                conversation.write(2, body(2) + f' @{conversation.revision + 1}')
            return
        downloading = args.get('source_token', [None])[0]
        if (kind == 'chunk' and index == 2 and args['offset'] == [str(2 * CHUNK)]
                and downloading not in interrupted):
            interrupted.add(downloading)
            conversation.write(2, body(2) + f' @{conversation.revision + 1}')
    conversation.writer = stream_final
    code, output = _inspect(conversation, capsys)
    assert code == 3, output.err
    assert 'LIVE: turn #3 is still being written' in output.out
    assert '--start-index 2 --max-turns 1' in output.out
    assert '[live, as of revision ' in output.out
    assert body(0, 600)[:20] in output.out and body(1, 600)[:20] in output.out


def test_streaming_final_turn_under_moving_headers_is_live_within_its_read_allowance(capsys):
    """Its tail refuses every read while an unrelated write lands after each probe.

    No two refusals share a header, so the two-observation rule never fires;
    the read allowance still bounds the work and classifies it live.
    """
    conversation = LiveConversation([body(0, 600), body(1, 600)])
    conversation.turns[1]['tail'] = True
    state = {'refused': False, 'probed': False}

    def move_after_each_probe(conversation, kind, index, args):
        if state['probed']:
            state['refused'] = state['probed'] = False
            conversation.write()
        elif state['refused'] and kind == 'header':
            state['probed'] = True
        elif kind == 'turn' and index == 1:
            state['refused'] = True
    conversation.writer = move_after_each_probe
    code, output = _inspect(conversation, capsys)
    assert code == 3, output.err
    assert '--- #2 [live]' in output.out
    assert 'LIVE: turn #2 is still being written; it changed or conflicted through every' in output.out
    assert f'coverage: 1 read, 1 unread; revision {conversation.revision};' in output.out
    refusals = sum(1 for kind, index, _ in conversation.requests if kind == 'turn' and index == 1)
    assert refusals <= 2 * inspector.SNAPSHOT_TURN_READS


def test_streaming_tail_without_any_complete_read_keeps_the_live_placeholder(capsys):
    conversation = LiveConversation([body(0, 600), body(1, 600)])
    conversation.turns[1]['tail'] = True
    code, output = _inspect(conversation, capsys)
    assert code == 3, output.err
    assert '--- #2 [live]' in output.out
    assert f'coverage: 1 read, 1 unread; revision {conversation.revision};' in output.out
    assert 'BEST-EFFORT' not in output.out


class SQLAuthorityConversation:
    """The SQL authority's real ``conversation.diagnostic`` behind the v4 wire.

    Reads run in a query-only SQLite transaction like the Sidecar; ``writer``
    commits real SQL writes on the same database between the inspector's reads.
    Storage conflicts map to the typed v4 problem the application route emits.
    """

    def __init__(self, tmp_path, contents):
        import sqlite3
        import threading
        from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
        from lib.storage_authority_sql.operations_pkg._common import _dump
        from lib.storage_authority_sql.schema import initialize_schema
        self.connection = sqlite3.connect(tmp_path / 'diagnostic.db', isolation_level=None,
                                          check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.session = SQLiteSession(self.connection)
        initialize_schema(self.session)
        self.lock = threading.Lock()
        self.dump = _dump
        self.session.execute(
            'INSERT INTO storage_conversations (id,user_id,title,created_at_ms,updated_at_ms,'
            'msg_count,rev,settings_json,messages_json) VALUES (?,?,?,?,?,?,?,?,?)',
            ('conversation', 1, 'SQL authority', 100, 100, len(contents), 7, _dump({}), _dump([])))
        for index, content in enumerate(contents):
            self.session.execute(
                'INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,'
                'presentation_id,lane_id,ordinal,actor,status,current_attempt_id,projection_json,'
                'projection_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'turn-{index}', 'conversation', 1, f'presentation-{index}', 'main', 2 * index + 1,
                 'assistant', 'completed', f'attempt-{index}', _dump({'content': content}), 1, 100, 100))
        self.served = Counter()
        self.writer = None

    def revision(self):
        return self.session.fetch_one("SELECT rev FROM storage_conversations WHERE id='conversation'")['rev']

    def content(self, index):
        row = self.session.fetch_one(
            'SELECT projection_json FROM storage_conversation_turns WHERE turn_id=?', (f'turn-{index}',))
        return json.loads(bytes(row['projection_json']) if not isinstance(row['projection_json'], str)
                          else row['projection_json'])['content']

    def write(self, index=None, content=None):
        if index is not None:
            self.session.execute(
                'UPDATE storage_conversation_turns SET projection_json=?, '
                'projection_revision=projection_revision+1, updated_at=updated_at+1 WHERE turn_id=?',
                (self.dump({'content': content}), f'turn-{index}'))
        self.session.execute("UPDATE storage_conversations SET rev=rev+1, updated_at_ms=updated_at_ms+1 "
                             "WHERE id='conversation'")

    def _query(self, payload):
        from lib.storage_authority_sql.operations_pkg._conversation_diagnostic import (
            _conversation_diagnostic)
        self.connection.execute('PRAGMA query_only=ON')
        self.connection.execute('BEGIN')
        try:
            return _conversation_diagnostic(self.session, payload)
        finally:
            self.connection.rollback()
            self.connection.execute('PRAGMA query_only=OFF')

    def serve(self, request):
        from lib.conversations.diagnostic import diagnostic_payload
        from lib.storage.errors import StorageError
        path = urlsplit(request.path)
        if path.path.endswith('/compactions'):
            return 200, {'compactions': [], 'count': 0}
        args = {key: values[0] for key, values in parse_qs(path.query).items()}
        options = {key: int(value) if key in ('revision', 'index', 'offset', 'limit') else value
                   for key, value in args.items()}
        with self.lock:
            if self.writer is not None:
                self.writer(self, options)
            try:
                result = self._query(diagnostic_payload('conversation', user_id=1, **options))
            except StorageError as exc:
                if exc.code != 'database_conflict':
                    raise
                return 409, CONFLICT
        if result is None:
            return 404, {'status': 404, 'code': 'diagnostic_conversation_not_found'}
        if options['kind'] == 'chunk':
            self.served[options['index']] += len(base64.b64decode(result['data']))
        return 200, result


def test_real_sql_authority_stays_readable_while_writes_land_between_reads(tmp_path, capsys):
    """Unrelated writes before every 5th of the first 40 requests; the final
    Turn is rewritten in the middle of its first two downloads."""
    authority = SQLAuthorityConversation(tmp_path, [body(0), body(1), body(2)])
    requests = final_chunks = 0

    def interleave(authority, options):
        nonlocal requests, final_chunks
        requests += 1
        if options['kind'] == 'chunk' and options['index'] == 2:
            final_chunks += 1
            if final_chunks in (2, 6):
                authority.write(2, body(2) + f' rewrite {final_chunks}')
                return
        if requests <= 40 and requests % 5 == 0:
            authority.write()
    authority.writer = interleave
    with api_server(authority.serve) as url:
        code = inspector.main(['conversation', '--api-url', url, '--no-logs', '--raw'])
    output = capsys.readouterr()
    assert code == 0, output.err
    final = authority.revision()
    assert f'coverage: 3 read, 0 unread; revision {final};' in output.out
    messages = json.loads(output.out.split('== Transcript ==', 1)[1].split('\n', 1)[1].split('\n  coverage:')[0])
    assert [message['content'] for message in messages] == [authority.content(index) for index in range(3)]
    assert ' rewrite ' in authority.content(2)
    # Revision-independent SQL tokens: settled turns are downloaded exactly once.
    for index in (0, 1):
        size = len(authority.dump({'content': body(index)}))
        assert size <= authority.served[index] <= size + inspector.SNAPSHOT_PASSES
