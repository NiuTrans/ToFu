"""Real HTTP fault boundaries for the read-only application conversation inspector."""
from __future__ import annotations

from contextlib import contextmanager

import base64

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from debug import inspect_conversation as inspector

pytestmark = pytest.mark.unit


@contextmanager
def api_server(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                result = callback(self)
                if result is None:
                    return
                status, data = result
                payload = {'data': data, 'meta': {}} if status == 200 else data
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json' if status == 200
                                 else 'application/problem+json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def pages(request, *, total=203, projection=None, patches=None, source_format='projection',
          revision=7):
    path = urlsplit(request.path)
    if path.path.endswith('/compactions'):
        return 200, {'compactions': [{'id': 'archive-1'}], 'count': 1}
    if '/compactions/' in path.path:
        assert parse_qs(path.query)['includeMessages'] == ['false']
        return 200, {'archive': {'id': 'archive-1', 'summary': 'retained summary', 'receipt': {'status': 'done'}}}
    assert path.path.endswith('/diagnostic'), 'Never hydrate legacy whole messages'
    args = parse_qs(path.query)
    kind = args['kind'][0]
    if kind == 'header':
        return 200, {'revision': revision, 'totalCount': total, 'turnNative': True,
                     'document': {'metadata': {'id': 'conversation', 'rev': revision,
                         'title': 'native document', 'settings': {}, 'msg_count': total},
                         'messages': []}}
    assert args['revision'] == [str(revision)]
    index = int(args['index'][0])
    source = projection if projection is not None else {'content': str(index)}
    if source_format == 'turn':
        source = {'turnId': f'turn-{index}', 'conversationId': 'conversation',
                  'laneId': 'main', 'projectionRevision': 1 + len(patches or []),
                  'projection': source}
    raw = json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode()

    source_token = hashlib.sha256(raw).hexdigest()
    if kind == 'turn':
        return 200, {'revision': revision, 'totalCount': total, 'index': index,
                     'turn': {'turnId': f'turn-{index}', 'conversationId': 'conversation',
                              'laneId': 'main', 'actor': 'human', 'ordinal': index * 3 + 2,
                              'projectionRevision': 1 + len(patches or [])},
                     'source': {'format': source_format, 'totalBytes': len(raw),
                                'token': source_token},
                     'patches': patches or []}
    assert kind == 'chunk'

    if args.get('source_token') != [source_token]:
        return 409, {'error': 'diagnostic source changed'}
    offset, limit = int(args['offset'][0]), int(args['limit'][0])
    data = raw[offset:offset + limit]
    end = offset + len(data)
    return 200, {'revision': revision, 'index': index, 'offset': offset,
                 'totalBytes': len(raw), 'sourceToken': source_token,
                 'nextOffset': end if end < len(raw) else None,
                 'data': base64.b64encode(data).decode()}


def test_api_cli_pages_and_uses_authorization_without_database_access(monkeypatch, capsys):
    seen = []
    def serve(request):
        seen.append((request.path, request.headers.get('Authorization')))
        return pages(request)
    monkeypatch.setattr(inspector, 'resolve_readonly_sqlite_authority', lambda *a, **k: pytest.fail('API must not open SQLite'))
    monkeypatch.setenv('INSPECT_TEST_TOKEN', 'secret-test-token')
    import tofu_dotenv
    monkeypatch.setattr(tofu_dotenv, 'read_dotenv_values', lambda *a, **k: pytest.fail('explicit API must not read project storage configuration'))
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--api-token-env', 'INSPECT_TEST_TOKEN']) == 0
    output = capsys.readouterr()
    assert 'native document' in output.out and 'retained summary' in output.out
    assert '203 message(s)' in output.out and 'authenticated API principal' in output.out
    assert 'secret-test-token' not in output.out + output.err
    assert all(token == 'Bearer secret-test-token' for _, token in seen)
    assert len(seen) == 2 * (inspector.HEAD_MESSAGES + inspector.TAIL_MESSAGES) + 4
    assert '23 read, 180 unread' in output.out
    assert '--- #204' not in output.out
    assert '--- #203' in output.out
    assert 'logs: omitted' in output.out


def test_api_cli_reports_failed_turn_settlement_without_attempt_rows(monkeypatch, capsys):
    def serve(request):
        status, body = pages(request, total=1, projection={'content': 'unfinished response'})
        if 'kind=turn' in request.path:
            body['turn'].update({
                'actor': 'agent', 'kind': 'response', 'status': 'failed',
                'currentAttemptId': 'attempt-synthetic',
                'settlement': {
                    'outcome': 'failed', 'cause': 'provider_stream_error',
                    'evidence': 'provider_stream_failure', 'streamState': 'premature_close',
                    'error': {'code': 'provider_stream_error', 'message': 'synthetic stream closed'},
                },
            })
        return status, body
    monkeypatch.setattr(inspector, 'resolve_readonly_sqlite_authority',
                        lambda *a, **k: pytest.fail('native diagnostic must not read attempt tables'))

    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 0

    output = capsys.readouterr().out
    assert '== Anomalies == (1 found' in output
    assert '[turn]' in output and 'status=failed' in output
    assert 'synthetic stream closed' in output and 'premature_close' in output
    assert 'attempt=attempt-' in output and 'turn=turn-0' in output
    assert 'REJECTED' not in output and '[attempt]' not in output


@pytest.mark.parametrize('mutation', ['revision', 'identity', 'cursor', 'empty'])
def test_api_refuses_inconsistent_pages(mutation):
    def serve(request):
        status, body = pages(request)
        if 'kind=turn' in request.path and mutation == 'identity':
            body['turn']['conversationId'] = 'another-conversation'
        if 'kind=chunk' in request.path:
            if mutation == 'revision': body['revision'] += 1
            if mutation == 'cursor': body['nextOffset'] = 99
            if mutation == 'empty': body['data'] = ''
        return status, body
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None), 'conversation', full=True)


@pytest.mark.parametrize('url', ['http://example.com', 'http://192.168.1.1', 'http://user:password@127.0.0.1',
                                 'http://127.0.0.1/path', 'http://127.0.0.1?token=secret', 'file:///tmp/data',
                                 'http://127.0.0.1:99999', 'http://127.0.0.1/#secret', 'http://127.0.0.1:0', 'http://@127.0.0.1'])
def test_api_rejects_ambiguous_or_nonlocal_authority(url):
    with pytest.raises(inspector.DiagnosticAPIError):
        inspector._DiagnosticAPI(url, None)


@pytest.mark.parametrize('status', [301, 302, 401, 403, 500])
def test_api_refusal_never_follows_redirect_or_echoes_remote_body(status):
    calls = []
    def serve(request):
        calls.append(request.path)
        request.send_response(status)
        request.send_header('Location', '/redirected')
        request.end_headers()
        request.wfile.write(b'secret-reflected-token')
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError) as error:
            inspector._DiagnosticAPI(url, 'secret-reflected-token').get('/api/v4/conversations/x')
    assert calls == ['/api/v4/conversations/x']
    assert 'secret-reflected-token' not in str(error.value)


@pytest.mark.parametrize('source_format', ['projection', 'turn'])
def test_api_reconstructs_single_field_larger_than_response_cap(monkeypatch, source_format):
    # 9 MiB UTF-8 string crosses code points and cannot fit a whole-message
    # response even at window=1. Every wire response stays below 128 KiB.
    big = '界' * (3 * 1024 * 1024)
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_RESPONSE_BYTES', 128 * 1024)
    seen = []
    def serve(request):
        seen.append(request.path)
        return pages(request, total=1, projection={'content': big}, source_format=source_format)
    with api_server(serve) as url:
        api = inspector._DiagnosticAPI(url, None)
        document, _ = inspector._load_api_document(api, 'conversation', full=False)
    assert document['messages'][0]['content'] == big
    assert document['diagnostic_unread_count'] == 0
    assert api.bytes_read < api.MAX_TOTAL_BYTES
    assert len([path for path in seen if 'kind=chunk' in path]) > 128
    assert seen[-1].endswith('?kind=header')


def test_api_response_and_aggregate_byte_budgets(monkeypatch):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_RESPONSE_BYTES', 100)
    with api_server(lambda request: (200, {'body': 'x' * 101})) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='byte budget'):
            inspector._DiagnosticAPI(url, None).get('/x')
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_TOTAL_BYTES', 50)
    with api_server(lambda request: (200, {})) as url:
        api = inspector._DiagnosticAPI(url, None)
        api.get('/x')
        api.get('/x')
        with pytest.raises(inspector.DiagnosticAPIError, match='byte budget'):
            api.get('/x')


def test_api_deadline_interrupts_continuously_dripping_body(monkeypatch):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'DEADLINE_SECONDS', .12)
    def serve(request):
        request.send_response(200)
        request.send_header('Content-Type', 'application/json')
        request.end_headers()
        for _ in range(100):
            request.wfile.write(b' ')
            request.wfile.flush()
            time.sleep(.01)
    with api_server(serve) as url:
        started = time.monotonic()
        with pytest.raises(inspector.DiagnosticAPIError):
            inspector._DiagnosticAPI(url, None).get('/x')
        assert time.monotonic() - started < 1.0


def test_api_missing_conversation_is_not_an_offline_store_claim(capsys):
    problem = {'status': 404, 'code': 'diagnostic_conversation_not_found'}
    with api_server(lambda request: (404, problem)) as url:
        assert inspector.main(['missing', '--api-url', url, '--no-logs']) == 2
    output = capsys.readouterr().out
    assert 'selected authority and owner scope' in output
    assert 'no known store' not in output


@pytest.mark.parametrize('problem', [
    {'status': 404, 'code': 'not_found'}, {}, [],
    {'code': 'diagnostic_conversation_not_found'},
    {'status': 200, 'code': 'diagnostic_conversation_not_found'},
    {'data': {'status': 404, 'code': 'diagnostic_conversation_not_found'}},
])
def test_api_missing_route_does_not_claim_missing_conversation(problem, capsys):
    with api_server(lambda request: (404, problem)) as url:
        assert inspector.main(['missing', '--api-url', url, '--no-logs']) == 1
    captured = capsys.readouterr()
    assert 'Diagnostic endpoint unavailable or incompatible' in captured.err
    assert 'update the running application and storage service' in captured.err
    assert '== Not found ==' not in captured.out


def test_api_missing_route_non_json_does_not_claim_missing_conversation(capsys):
    def serve(request):
        request.send_response(404)
        request.send_header('Content-Type', 'text/html')
        request.end_headers()
        request.wfile.write(b'Not Found')
    with api_server(serve) as url:
        assert inspector.main(['missing', '--api-url', url, '--no-logs']) == 1
    captured = capsys.readouterr()
    assert 'Diagnostic endpoint unavailable or incompatible' in captured.err
    assert '== Not found ==' not in captured.out


def test_api_missing_problem_still_obeys_byte_budget(monkeypatch):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_RESPONSE_BYTES', 100)
    problem = {'status': 404, 'code': 'diagnostic_conversation_not_found',
               'detail': 'x' * 200}
    with api_server(lambda request: (404, problem)) as url:
        with pytest.raises(inspector.DiagnosticResponseTooLarge, match='byte budget'):
            inspector._DiagnosticAPI(url, None).get('/x', missing_ok=True)


def test_api_deadline_interrupts_dripping_headers(monkeypatch):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'DEADLINE_SECONDS', .12)
    def serve(request):
        request.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
        for _ in range(100):
            request.wfile.write(b'x')
            request.wfile.flush()
            time.sleep(.01)
    with api_server(serve) as url:
        started = time.monotonic()
        with pytest.raises(inspector.DiagnosticAPIError):
            inspector._DiagnosticAPI(url, None).get('/x')
        assert time.monotonic() - started < 1.0


def test_api_request_budget_and_short_body_fail_closed(monkeypatch):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_REQUESTS', 1)
    with api_server(lambda request: (200, {})) as url:
        api = inspector._DiagnosticAPI(url, None)
        api.get('/x')
        with pytest.raises(inspector.DiagnosticAPIError, match='request budget'):
            api.get('/x')
    def short_body(request):
        request.send_response(200)
        request.send_header('Content-Type', 'application/json')
        request.send_header('Content-Length', '100')
        request.end_headers()
        request.wfile.write(b'{"data":{}}')
    with api_server(short_body) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='declared length'):
            inspector._DiagnosticAPI(url, None).get('/x')


def test_api_rejects_offline_owner_override_and_unset_token(monkeypatch, capsys):
    with pytest.raises(SystemExit) as error:
        inspector.main(['conversation', '--api-url', 'http://127.0.0.1:1', '--user-id', '2'])
    assert error.value.code == 2
    monkeypatch.delenv('INSPECT_MISSING_TOKEN', raising=False)
    assert inspector.main(['conversation', '--api-url', 'http://127.0.0.1:1',
        '--api-token-env', 'INSPECT_MISSING_TOKEN']) == 1
    assert 'environment variable is unset' in capsys.readouterr().err


@pytest.mark.parametrize('value', [float('inf'), float('nan'), 10**30])
def test_diagnostic_renders_unrepresentable_timestamps_without_crashing(value):
    assert 'unrepresentable timestamp' in inspector._format_ts(value)


def test_api_mode_scans_explicit_logs_dir_with_original_values(tmp_path, capsys):
    (tmp_path / 'app.log').write_text(
        'conversation bootstrap ok\n'
        'conversation failed authorization=Bearer secret-log-token retrying\n',
        encoding='utf-8')
    (tmp_path / 'access.log').write_text(
        'GET /api/v4/conversations/conversation 200\n', encoding='utf-8')
    with api_server(pages) as url:
        assert inspector.main(['conversation', '--api-url', url,
                               '--logs-dir', str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert '== Logs ==' in output
    assert 'app.log: 2 line(s)' in output
    assert 'conversation failed' in output
    assert 'secret-log-token' in output
    assert 'logs: omitted' not in output


def test_api_mode_log_match_filters_before_the_cap_and_keeps_file_headers(tmp_path, capsys):
    (tmp_path / 'app.log').write_text(
        '[ModelReplay] conv=conversa restore=checkpoint_missing\n'
        '[Chat] conversation queued\n'
        '[Translate] committed field=content conv=conversa turn=t1\n'
        '[Translate] committed field=content conv=conversa turn=t2\n', encoding='utf-8')
    with api_server(pages) as url:
        assert inspector.main(['conversation', '--api-url', url, '--logs-dir', str(tmp_path),
                               '--logs', '2', '--log-match', r'ModelReplay|Translate\] committed']) == 0
    output = capsys.readouterr().out
    logs = output.split('== Logs ==', 1)[1].splitlines()
    assert logs == [r' (--log-match ModelReplay|Translate\] committed)',
                    '--- app.log: 2 line(s) (last 4 MiB)',
                    '[Translate] committed field=content conv=conversa turn=t1',
                    '[Translate] committed field=content conv=conversa turn=t2']


JSON_KEYS = {'conversationId', 'found', 'exitCode', 'document', 'messages',
             'anomalies', 'compactionArchives'}


def test_api_json_mode_prints_one_document_with_every_read_turn(tmp_path, capsys):
    (tmp_path / 'app.log').write_text('conversation bootstrap ok\nunrelated line\n',
                                      encoding='utf-8')
    with api_server(lambda request: pages(request, total=30)) as url:
        assert inspector.main(['conversation', '--api-url', url,
                               '--logs-dir', str(tmp_path), '--json']) == 0
    captured = capsys.readouterr()
    assert captured.out.count('\n') == 1 and not captured.err
    payload = json.loads(captured.out)
    assert set(payload) == JSON_KEYS | {'logs'}
    assert (payload['conversationId'], payload['found'], payload['exitCode']) == (
        'conversation', True, 0)
    assert 'messages' not in payload['document']
    assert payload['document']['metadata']['title'] == 'native document'
    assert payload['document']['diagnostic_unread_count'] == 0
    # Like --raw, --json reads every turn instead of the head/tail selection.
    assert [message['content'] for message in payload['messages']] == [
        str(index) for index in range(30)]
    assert payload['compactionArchives'][0]['summary'] == 'retained summary'
    assert payload['anomalies'] == []
    assert payload['logs'] == ['conversation bootstrap ok']


def test_api_json_mode_absence_errors_and_partial_evidence(monkeypatch, capsys):
    problem = {'status': 404, 'code': 'diagnostic_conversation_not_found'}
    with api_server(lambda request: (404, problem)) as url:
        assert inspector.main(['missing', '--api-url', url, '--json']) == 2
    assert json.loads(capsys.readouterr().out) == {
        'conversationId': 'missing', 'found': False, 'exitCode': 2}

    with api_server(lambda request: (404, {})) as url:
        assert inspector.main(['missing', '--api-url', url, '--json']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Diagnostic endpoint unavailable or incompatible' in captured.err

    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_TOTAL_BYTES', 3 * 1024 * 1024)
    with api_server(lambda request: pages(
            request, total=1, projection={'content': 'x' * (3 * 1024 * 1024)})) as url:
        assert inspector.main(['conversation', '--api-url', url, '--json',
                               '--no-anomalies']) == 3
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == JSON_KEYS - {'anomalies'}
    assert payload['exitCode'] == 3 and payload['messages'] == []
    assert payload['document']['diagnostic_resume_index'] == 0
    # The Turn alone (4.05 MiB of wire reads) exceeds the whole 3 MiB allowance.
    assert payload['document']['diagnostic_resume_total_mib'] == 5


def _rule_note(llm_round):
    return {'type': 'system_note', 'noteKind': 'system-intervention',
            'reason': 'project_scoped_rules', 'llmRound': llm_round,
            'text': 'Project Intelligence — AGENTS.md\nSource: /repo/AGENTS.md\n'
                    'Applies only within: /repo\n\n# Rules\n- keep it small'}


def _context_pages(request):
    index = int(parse_qs(urlsplit(request.path).query).get('index', ['0'])[0])
    segments = {0: [_rule_note(0)],
                5: [{'type': 'system_note', 'noteKind': 'intent-stall', 'llmRound': 1,
                     'text': 'Continue the plan.'}, _rule_note(2)]}.get(index, [])
    return pages(request, total=30,
                 projection={'content': f'turn {index}', 'segments': segments})


def test_api_context_reads_every_turn_and_keeps_the_default_transcript_view(capsys):
    with api_server(_context_pages) as url:
        assert inspector.main(['conversation', '--api-url', url, '--context']) == 0
    output = capsys.readouterr().out
    section = output.split('== Context injections ==\n', 1)[1].split('\n\n', 1)[0]
    assert section.splitlines() == [
        '  #1 r0 project_scoped_rules: 5 line(s) source: /repo/AGENTS.md',
        '  #6 r1 intent-stall: 1 line(s)',
        '  #6 r2 project_scoped_rules: 5 line(s) source: /repo/AGENTS.md (repeat of #1 r0)',
        '  3 note(s), 2 distinct text(s), 1 repeat(s)',
    ]
    transcript = output.split('== Transcript ==', 1)[1].split('coverage:', 1)[0]
    assert '(30 message(s), head 3 + tail 20)' in transcript
    assert '--- #3 [' in transcript and '--- #4 [' not in transcript
    assert '--- #6 [' not in transcript and '--- #11 [' in transcript
    assert '… <7 message(s) omitted; use --full>' in transcript
    assert 'unread message' not in transcript
    assert 'coverage: 30 read, 0 unread' in output

    with api_server(_context_pages) as url:
        assert inspector.main(['conversation', '--api-url', url, '--context', '--json']) == 0
    records = json.loads(capsys.readouterr().out)['contextInjections']
    assert [(record['messageNumber'], record['llmRound'], record['reason'] or record['noteKind'],
             record['nonBlankLines'], record['sources'], record['repeatOf'])
            for record in records] == [
        (1, 0, 'project_scoped_rules', 5, ['/repo/AGENTS.md'], None),
        (6, 1, 'intent-stall', 1, [], None),
        (6, 2, 'project_scoped_rules', 5, ['/repo/AGENTS.md'],
         {'messageNumber': 1, 'llmRound': 0}),
    ]


def test_api_mode_without_logs_dir_keeps_logs_omitted(capsys):
    with api_server(pages) as url:
        assert inspector.main(['conversation', '--api-url', url]) == 0
    assert 'logs: omitted' in capsys.readouterr().out


def test_logs_dir_requires_an_existing_directory(tmp_path):
    with pytest.raises(SystemExit) as error:
        inspector.main(['conversation', '--api-url', 'http://127.0.0.1:1',
                        '--logs-dir', str(tmp_path / 'absent')])
    assert error.value.code == 2


def test_logs_dir_and_no_logs_are_contradictory(tmp_path):
    with pytest.raises(SystemExit) as error:
        inspector.main(['conversation', '--api-url', 'http://127.0.0.1:1',
                        '--logs-dir', str(tmp_path), '--no-logs'])
    assert error.value.code == 2


def test_full_read_and_explicit_resume_range_keep_true_indices():
    with api_server(pages) as url:
        document, _ = inspector._load_api_document(
            inspector._DiagnosticAPI(url, None), 'conversation', full=True)
        resumed, _ = inspector._load_api_document(
            inspector._DiagnosticAPI(url, None), 'conversation', full=False,
            start_index=100, max_turns=2)
    assert len(document['messages']) == 203
    assert document['diagnostic_unread_count'] == 0
    assert resumed['diagnostic_message_indices'] == [100, 101]
    assert [m['content'] for m in resumed['messages']] == ['100', '101']
    text = '\n'.join(inspector._render_transcript(resumed, full=False, raw=False))
    assert '--- #101' in text and '--- #102' in text
    assert '100 unread message(s)' in text


def test_patch_chain_is_reconstructed_using_storage_codec():
    from lib.turn_projection_patch import build_projection_patch
    base = {'content': '起点', 'segments': [{'text': 'first'}]}
    target = {'content': '起点 + continuation', 'segments': [{'text': 'first'}, {'text': 'last'}]}
    patch = build_projection_patch(base, target, base_revision=1, target_revision=2)
    with api_server(lambda request: pages(request, total=1, projection=base,
                    patches=[{'projectionRevision': 2, 'projectionPatch': patch}])) as url:
        document, _ = inspector._load_api_document(inspector._DiagnosticAPI(url, None),
                                                   'conversation', full=True)
    assert document['messages'][0]['content'] == target['content']
    assert document['messages'][0]['segments'] == target['segments']


def test_final_header_revision_change_is_not_reported_as_consistent(monkeypatch):
    """A write lands before every header after the first, so no witness ever closes."""
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    revision = 7
    headers = 0
    seen = []
    def serve(request):
        nonlocal revision, headers
        seen.append(request.path)
        if 'kind=header' in request.path:
            headers += 1
            revision += headers > 1
        return pages(request, total=1, revision=revision)
    with api_server(serve) as url:
        document, _ = inspector._load_api_document(inspector._DiagnosticAPI(url, None),
                                                   'conversation', full=True)
    assert document['diagnostic_snapshot'] == 'best-effort'
    assert document['diagnostic_revision'] == revision
    assert document['diagnostic_revision_bounds'] == [revision - 1, revision]
    assert document['diagnostic_turn_revisions'] == [revision - 1]
    assert [message['content'] for message in document['messages']] == ['0']
    # Downloaded once, then re-verified by one-byte witnesses at each new revision.
    assert sum('kind=turn' in path for path in seen) == 1
    assert sum('kind=chunk' in path for path in seen) == inspector.SNAPSHOT_PASSES


def _sized_pages(sizes):
    def serve(request):
        index = int(parse_qs(urlsplit(request.path).query).get('index', ['0'])[0])
        return pages(request, total=len(sizes), projection={'content': 'x' * sizes[index]})
    return serve


def _resume_line(output):
    [line] = [line for line in output.splitlines() if line.startswith('  resume: ')]
    return line


def _resume_flags(output):
    return _resume_line(output).removeprefix('  resume: ').split(' (', 1)[0].split()


def test_unaffordable_turn_reports_unread_and_resume_without_downloading(monkeypatch, capsys):
    # A Turn larger than the whole allowance: paging by turn cannot read it.
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_TOTAL_BYTES', 1024 * 1024)
    seen = []
    serve = _sized_pages([1000, 800_000])
    def record(request):
        seen.append(request.path)
        return serve(request)
    with api_server(record) as url:
        command = ['conversation', '--api-url', url, '--no-logs']
        assert inspector.main(command) == 3
        output = capsys.readouterr().out
        assert 'INCOMPLETE: Turn 1 alone needs' in output
        assert '1 read, 1 unread' in output
        assert not any('kind=chunk' in path and '&index=1&' in path for path in seen)
        assert seen[-1].endswith('?kind=header')
        resume = _resume_flags(output)
        assert resume == ['--start-index', '1', '--max-turns', '1', '--api-max-total-mib', '2']
        # Paging alone fails the same way; the allowance derived from observed sizes reads it.
        assert inspector.main(command + resume[:4]) == 3
        assert '0 read, 2 unread' in capsys.readouterr().out
        assert inspector.main(command + resume) == 0
        output = capsys.readouterr().out
        assert '1 read, 1 unread' in output and 'INCOMPLETE' not in output


@pytest.mark.parametrize('sizes, allowance, read', [
    ([300_000] * 3, [], 2),
    # Each Turn exceeds the default allowance: the resume keeps the explicit one.
    ([800_000] * 2, ['--api-max-total-mib', '2'], 1),
])
def test_budget_cut_at_turn_boundary_resumes_by_paging(monkeypatch, capsys, sizes, allowance, read):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_TOTAL_BYTES', 1024 * 1024)
    with api_server(_sized_pages(sizes)) as url:
        command = ['conversation', '--api-url', url, '--no-logs']
        assert inspector.main(command + allowance) == 3
        output = capsys.readouterr().out
        assert 'exceeds the remaining diagnostic byte/request budget' in output
        assert f'{read} read, {len(sizes) - read} unread' in output
        resume = _resume_flags(output)
        assert resume == ['--start-index', str(read), '--max-turns', '1', *allowance]
        assert inspector.main(command + resume) == 0
        output = capsys.readouterr().out
        assert f'1 read, {len(sizes) - 1} unread' in output and 'INCOMPLETE' not in output


def test_turn_beyond_the_maximum_allowance_has_no_resume(capsys):
    def serve(request):
        status, body = pages(request, total=1)
        if 'kind=turn' in request.path:
            body['source']['totalBytes'] = inspector.API_TOTAL_MIB_MAX * 1024 * 1024
        return status, body
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 3
    output = capsys.readouterr().out
    assert 'INCOMPLETE: Turn 0 alone needs' in output
    resume = _resume_line(output)
    assert '--start-index' not in resume
    assert f'{inspector.API_TOTAL_MIB_MAX} MiB maximum --api-max-total-mib' in resume


def test_oversize_without_content_length_counts_bytes(monkeypatch):
    def serve(request):
        request.send_response(200)
        request.send_header('Content-Type', 'application/json')
        request.end_headers()
        request.wfile.write(b'x' * 200)
    with api_server(serve) as url:
        api = inspector._DiagnosticAPI(url, None)
        monkeypatch.setattr(api, 'MAX_RESPONSE_BYTES', 100)
        with pytest.raises(inspector.DiagnosticBudgetError, match='byte budget'):
            api.get('/test')
        assert api.bytes_read == 101


def test_chunk_gap_and_patch_revision_error_fail_closed():
    def broken(request):
        status, body = pages(request, total=1)
        if 'kind=turn' in request.path:
            body['patches'] = [{'projectionRevision': 99, 'projectionPatch': {}}]
        return status, body
    with api_server(broken) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='malformed'):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None),
                                         'conversation', full=True)


def test_archive_budget_does_not_hide_verified_transcript_or_crash(monkeypatch, capsys):
    def budget(*args, **kwargs):
        raise inspector.DiagnosticBudgetError('archive metadata exceeds byte budget')
    monkeypatch.setattr(inspector, '_load_api_archives', budget)
    with api_server(lambda request: pages(request, total=1)) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 3
    output = capsys.readouterr()
    assert '--- #1' in output.out
    assert 'INCOMPLETE' in output.out
    assert 'archives loaded: 0 of unknown' in output.out
    assert 'Traceback' not in output.err


@pytest.mark.parametrize('field', ['turnId', 'projectionRevision'])
def test_inline_turn_source_must_match_descriptor(field):
    def serve(request):
        status, body = pages(request, total=1, source_format='turn')
        if 'kind=chunk' in request.path:
            encoded = json.loads(base64.b64decode(body['data']))
            encoded[field] = 'turn-9' if field == 'turnId' else 2
            raw = json.dumps(encoded, ensure_ascii=False, separators=(',', ':')).encode()
            assert len(raw) == body['totalBytes']
            body['data'] = base64.b64encode(raw).decode()
        return status, body
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='malformed diagnostic projection'):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None), 'conversation', full=True)


@pytest.mark.parametrize('mutation', ['duplicate', 'reordered'])
def test_diagnostic_rejects_duplicate_identity_or_nonincreasing_ordinals(mutation):
    def serve(request):
        status, body = pages(request, total=2)
        if 'kind=turn' in request.path and 'index=1' in request.path:
            if mutation == 'duplicate':
                body['turn']['turnId'] = 'turn-0'
            else:
                body['turn']['ordinal'] = 2
        return status, body
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='duplicate or out-of-order'):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None), 'conversation', full=True)


@pytest.mark.parametrize('mutation', ['missing_descriptor', 'malformed_descriptor', 'missing_echo', 'wrong_echo', 'replacement'])
def test_source_generation_is_bound_across_chunks(mutation):
    chunks = 0
    def serve(request):
        nonlocal chunks
        if 'kind=chunk' in request.path:
            chunks += 1
        projection = {'content': ('B' if mutation == 'replacement' and chunks > 1 else 'A') * 90000}
        status, body = pages(request, total=1, projection=projection)
        if mutation == 'missing_descriptor' and 'kind=turn' in request.path:
            body['source'].pop('token')
        if mutation == 'malformed_descriptor' and 'kind=turn' in request.path:
            body['source']['token'] = 'not-a-token'
        if status == 200 and 'kind=chunk' in request.path:
            if mutation == 'missing_echo':
                body.pop('sourceToken')
            elif mutation == 'wrong_echo':
                body['sourceToken'] = '0' * 64
        return status, body
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='source token|non-contiguous|HTTP 409'):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None), 'conversation', full=True)
    if mutation == 'replacement':
        assert chunks == 2


def test_restored_projection_transform_runs_after_patch_replay():
    from lib.turn_projection_patch import build_projection_patch
    base = {'content': 'restored', '_streaming': True}
    target = {**base, 'toolRounds': [{'status': 'running'}, {'status': 'success'}, {'status': []}],
              'timingTrace': {'status': 'running', 'running': True},
              'segments': [{'type': 'tool_use', '_runId': 'old-run', 'result': {'status': 'running'}}]}
    patch = build_projection_patch(base, target, base_revision=1, target_revision=2)
    def serve(request):
        status, body = pages(request, total=1, projection=base,
            patches=[{'projectionRevision': 2, 'projectionPatch': patch}])
        if 'kind=turn' in request.path:
            body['projectionTransform'] = 'terminalize_restored'
        return status, body
    with api_server(serve) as url:
        document, _ = inspector._load_api_document(inspector._DiagnosticAPI(url, None), 'conversation', full=True)
    message = document['messages'][0]
    assert '_streaming' not in message
    assert [row['status'] for row in message['toolRounds']] == ['aborted', 'success', 'aborted']
    assert message['timingTrace'] == {'status': 'aborted', 'running': False}
    assert message['segments'] == [{'type': 'tool_use', 'result': {'status': 'aborted'}}]


@pytest.mark.parametrize('bound', ['bytes', 'requests'])
def test_archive_actual_budget_consumption_preserves_final_witness(bound):
    seen = []
    def serve(request):
        seen.append(request.path)
        if bound == 'bytes' and '/compactions/archive-1' in request.path:
            # No Content-Length: exercise actual consumed bytes, not early refusal.
            request.send_response(200)
            request.send_header('Content-Type', 'application/json')
            request.end_headers()
            request.wfile.write(json.dumps({'data': {'archive': {'summary': 'x' * (3 * 1024 * 1024)}}}).encode())
            return None
        return pages(request, total=1)
    with api_server(serve) as url:
        api = inspector._DiagnosticAPI(url, None)
        if bound == 'bytes':
            api.MAX_TOTAL_BYTES = 2 * 1024 * 1024 + 8192
        else:
            api.MAX_REQUESTS = 5  # header, turn, chunk, archive list, final header
        document, _ = inspector._load_api_document(api, 'conversation', full=True)
    assert document['messages'][0]['content'] == '0'
    assert document['diagnostic_unread_count'] == 0
    assert document['diagnostic_incomplete']
    assert document['diagnostic_archive_catalog_count'] == 'unknown'
    assert seen[-1].endswith('?kind=header')
    assert api.bytes_read <= api.MAX_TOTAL_BYTES
    assert api.requests <= api.MAX_REQUESTS


def test_explicit_total_allowance_and_response_error_are_preserved(monkeypatch, capsys):
    def load(api, conv_id, *, full, **kwargs):
        assert api.MAX_TOTAL_BYTES == 128 * 1024 * 1024
        assert api.MAX_REQUESTS >= api.MAX_TOTAL_BYTES // (64 * 1024)
        assert api.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
        return None, []
    monkeypatch.setattr(inspector, '_load_api_document', load)
    assert inspector.main(['conversation', '--api-url', 'http://127.0.0.1:1',
                           '--api-max-total-mib', '128']) == 2
    capsys.readouterr()
    with api_server(lambda request: (200, {'text': 'x' * 1000})) as url:
        api = inspector._DiagnosticAPI(url, None)
        api.MAX_RESPONSE_BYTES = 100
        with pytest.raises(inspector.DiagnosticResponseTooLarge):
            api.get('/test')


@pytest.mark.parametrize('value', ['0', '-1', '1025', 'unlimited', '1.5'])
def test_explicit_total_allowance_remains_bounded(value):
    with pytest.raises(SystemExit) as error:
        inspector.main(['conversation', '--api-url', 'http://127.0.0.1:1', '--api-max-total-mib', value])
    assert error.value.code == 2


def test_api_result_surfaces_preserve_retained_turn_inputs(capsys):
    projection = {'role': 'assistant', 'content': 'Done', 'toolRounds': [{
        'toolName': 'get_agent_result', 'toolCallId': 'call-1', 'status': 'done',
        'toolContent': 'Report HEAD\napi_key=secret-result-token\nReport TAIL',
        'results': [{'snippet': 'Frontend secondary caption'}],
        'toolResultEvidence': {'artifactRef': 'tool-result:' + 'a' * 64,
                               'rawBytes': 1000, 'visibleBytes': 70},
    }]}
    with api_server(lambda request: pages(request, total=1, projection=projection)) as url:
        assert inspector.main(['conversation', '--api-url', url, '--tool-results',
                               '--tool-call-id', 'call-1', '--no-logs']) == 0
    output = capsys.readouterr()
    assert 'Tool result surfaces' in output.out
    assert 'model: retained · toolContent' in output.out
    assert 'frontend: retained · toolContent' in output.out
    assert 'Frontend secondary caption' in output.out
    assert 'Report HEAD' in output.out and 'Report TAIL' in output.out
    assert 'secret-result-token' in output.out + output.err
    assert 'tool-result:' + 'a' * 64 in output.out


def test_result_surfaces_observer_never_promotes_frontend_metadata_to_delivery():
    lines = inspector._render_result_surfaces({'messages': [{'toolRounds': [{
        'toolName': 'read_files', 'toolCallId': 'missing',
        'results': [{'snippet': 'User-visible caption'}],
    }]}]})
    output = '\n'.join(lines)
    assert 'model: missing' in output and 'missing_model_result' in output
    assert 'User-visible caption' in output


@pytest.mark.parametrize('conflict_at', ['turn', 'chunk', 'witness'])
def test_diagnostic_conflict_never_mixes_sources(conflict_at, monkeypatch):
    """Both Turns change at one conflict; only the new content is ever reported.

    ``turn``/``chunk``: the sources change at an unchanged revision (native reserves
    that for a streaming tail), so the whole verification window is distrusted.
    ``witness``: an ordinary write advances the revision before the final header.
    Each changed Turn is downloaded again; nothing read before the conflict survives.
    """
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    revision, headers, conflicted = 7, 0, False
    seen = []
    def serve(request):
        nonlocal revision, headers, conflicted
        seen.append(request.path)
        args = parse_qs(urlsplit(request.path).query)
        kind = args.get('kind', [''])[0]
        headers += kind == 'header'
        if not conflicted and ((kind == conflict_at and args.get('index') == ['1'])
                               or (conflict_at == 'witness' and kind == 'header' and headers == 2)):
            conflicted = True
            if kind != 'header':
                return 409, CONFLICT
            revision += 1
        if kind in ('turn', 'chunk') and args['revision'] != [str(revision)]:
            return 409, CONFLICT
        status, body = pages(request, total=2, revision=revision,
                             projection={'content': 'new' if conflicted else 'old'})
        # A stale source token is a typed storage conflict on every authority.
        return (409, CONFLICT) if status == 409 else (status, body)
    with api_server(serve) as url:
        api = inspector._DiagnosticAPI(url, None)
        deadline = api.deadline
        document, _ = inspector._load_api_document(api, 'conversation', full=True)
    assert conflicted
    assert [message['content'] for message in document['messages']] == ['new', 'new']
    assert document['diagnostic_message_indices'] == [0, 1]
    assert document['diagnostic_snapshot'] == 'consistent'
    assert document['diagnostic_revision'] == revision
    assert api.requests == len(seen)
    assert api.deadline == deadline
    assert api.reserved_bytes == api.reserved_requests == 0
    assert sum('kind=turn' in path and 'index=0' in path for path in seen) == 2


@pytest.mark.parametrize('status,problem', [
    (409, {'status': 409, 'code': 'conflict', 'detail': 'unrelated'}),
    (409, {'status': 409, 'code': 'other', 'detail': 'database_conflict'}),
    (409, {'error': 'database_conflict'}),
    (401, {'status': 409, 'code': 'conflict', 'detail': 'database_conflict'}),
    (403, {'status': 409, 'code': 'conflict', 'detail': 'database_conflict'}),
    (500, {'status': 409, 'code': 'conflict', 'detail': 'database_conflict'}),
])
def test_only_typed_diagnostic_conflicts_restart(status, problem):
    seen = []
    def serve(request):
        seen.append(request.path)
        if 'kind=turn' in request.path:
            return status, problem
        return pages(request, total=1)
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match=f'HTTP {status}'):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None), 'conversation', full=True)
    assert len(seen) == 2


CONFLICT = {'status': 409, 'code': 'conflict', 'detail': 'database_conflict'}


def test_persistent_revision_churn_exhausts_without_partial_output(monkeypatch, capsys):
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    revision = 7
    headers = 0
    def serve(request):
        nonlocal revision, headers
        if 'kind=header' in request.path:
            headers += 1
            revision += 1
            return pages(request, total=1, revision=revision)
        return 409, CONFLICT
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 1
    output = capsys.readouterr()
    # Every probe sees a newer revision: each pass follows the move a bounded
    # number of times, and with no turn ever read the run fails without output.
    assert headers == 1 + inspector.SNAPSHOT_PASSES * (inspector.SNAPSHOT_MOVE_RETRIES + 1)
    assert 'no selected turn could be read' in output.err
    assert 'writes settle' in output.err
    assert 'written continuously' in output.err
    assert not output.out


def test_persistent_final_turn_conflict_reports_verified_snapshot_and_live_turn(
        monkeypatch, capsys):
    """A streaming final Turn conflicts at an unchanged revision until it settles."""
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    seen = []
    def serve(request):
        seen.append(request.path)
        if 'kind=turn' in request.path and 'index=1' in request.path:
            return 409, CONFLICT
        return pages(request, total=2)
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 3
    output = capsys.readouterr()
    assert not output.err
    assert '--- #1 [' in output.out and '\n0\n' in output.out
    assert '--- #2 [live]' in output.out
    assert 'LIVE: turn #2' in output.out
    assert '--start-index 1 --max-turns 1' in output.out
    # Two unchanged-header observations classify it; the rest of the snapshot
    # is re-verified by a one-byte witness instead of being downloaded again.
    assert sum('kind=turn' in path and 'index=1' in path for path in seen) == 2
    assert sum('kind=turn' in path and 'index=0' in path for path in seen) == 1
    assert sum('kind=chunk' in path and 'index=0' in path for path in seen) == 2
    assert sum('kind=header' in path for path in seen) == 4
    assert 'coverage: 1 read, 1 unread; revision 7;' in output.out


def test_transient_final_turn_conflict_is_not_reported_live(monkeypatch, capsys):
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    conflicts = 0
    def serve(request):
        nonlocal conflicts
        if 'kind=turn' in request.path and 'index=1' in request.path and not conflicts:
            conflicts += 1
            return 409, CONFLICT
        return pages(request, total=2)
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 0
    output = capsys.readouterr()
    assert conflicts == 1
    assert '[live]' not in output.out and 'LIVE:' not in output.out
    assert '--- #2 [' in output.out and '\n1\n' in output.out


def test_persistent_nonfinal_turn_conflict_is_never_classified_live(monkeypatch, capsys):
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    seen = []
    def serve(request):
        seen.append(request.path)
        if 'kind=turn' in request.path and 'index=0' in request.path:
            return 409, CONFLICT
        return pages(request, total=2)
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 1
    output = capsys.readouterr()
    assert 'Turn 0 conflicted at unchanged revision 7' in output.err
    assert 'not write churn' in output.err
    assert '--start-index 0 --max-turns 1' in output.err
    assert sum('kind=turn' in path and 'index=0' in path for path in seen) == inspector.SNAPSHOT_TURN_READS
    assert not output.out


def test_live_classification_is_discarded_when_the_revision_moves(monkeypatch):
    monkeypatch.setattr(inspector, 'SNAPSHOT_RETRY_DELAYS_S', (0, 0))
    revision, refused = 7, 0
    def serve(request):
        nonlocal revision, refused
        args = parse_qs(urlsplit(request.path).query)
        kind = args.get('kind', [''])[0]
        if refused == 2 and kind in ('turn', 'chunk'):
            # After the stream was classified live at revision 7, a structural
            # write retires its text tail and advances the revision to 8.
            revision, refused = 8, 3
        if kind in ('turn', 'chunk') and args['revision'] != [str(revision)]:
            return 409, CONFLICT
        if revision == 7 and kind == 'turn' and args.get('index') == ['1']:
            refused += 1
            return 409, CONFLICT
        return pages(request, total=2, revision=revision)
    with api_server(serve) as url:
        document, _ = inspector._load_api_document(
            inspector._DiagnosticAPI(url, None), 'conversation', full=True)
    assert refused == 3
    assert 'diagnostic_live_index' not in document
    assert document['diagnostic_revision'] == 8
    assert document['diagnostic_snapshot'] == 'consistent'
    assert [message['content'] for message in document['messages']] == ['0', '1']


def test_api_server_error_is_not_reported_as_an_authorization_refusal():
    def serve(request):
        return 500, {'status': 500, 'code': 'internal_error'}
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError) as error:
            inspector._DiagnosticAPI(url, None).get(
                '/api/v4/conversations/x/diagnostic?kind=header')
    message = str(error.value)
    assert 'HTTP 500' in message and 'server-side' in message
    assert 'authorization' not in message


@pytest.mark.parametrize('bound', ['bytes', 'requests', 'deadline'])
def test_diagnostic_retry_keeps_cumulative_budgets(bound):
    seen = []
    api = None
    wire_bytes = 0
    def serve(request):
        nonlocal wire_bytes
        seen.append(request.path)
        if 'kind=turn' in request.path:
            status, body = 409, {'status': 409, 'code': 'conflict', 'detail': 'database_conflict'}
        else:
            status, body = pages(request, total=1)
        wire_bytes += len(json.dumps({'data': body, 'meta': {}} if status == 200 else body).encode())
        return status, body
    with api_server(serve) as url:
        api = inspector._DiagnosticAPI(url, None)
        deadline = api.deadline
        original_get = api.get
        def get(*args, **kwargs):
            try:
                return original_get(*args, **kwargs)
            except inspector.DiagnosticSnapshotConflict:
                # Exhaust the shared remaining allowance precisely at retry.
                if bound == 'bytes':
                    api.MAX_TOTAL_BYTES = api.bytes_read
                elif bound == 'requests':
                    api.MAX_REQUESTS = api.requests
                else:
                    api.deadline = 0
                raise
        api.get = get
        with pytest.raises(inspector.DiagnosticBudgetError, match='budget'):
            inspector._load_api_document(api, 'conversation', full=True)
    assert len(seen) == api.requests == 2
    assert api.bytes_read == wire_bytes  # includes the discarded 409 body
    assert api.reserved_bytes == api.reserved_requests == 0
    assert api.deadline == (0 if bound == 'deadline' else deadline)
