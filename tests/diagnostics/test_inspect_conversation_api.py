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


def pages(request, *, total=203, projection=None, patches=None, source_format='projection'):
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
        return 200, {'revision': 7, 'totalCount': total, 'turnNative': True,
                     'document': {'metadata': {'id': 'conversation', 'rev': 7,
                         'title': 'native document', 'settings': {}, 'msg_count': total},
                         'messages': []}}
    assert args['revision'] == ['7']
    index = int(args['index'][0])
    source = projection if projection is not None else {'content': str(index)}
    if source_format == 'turn':
        source = {'turnId': f'turn-{index}', 'conversationId': 'conversation',
                  'laneId': 'main', 'projectionRevision': 1 + len(patches or []),
                  'projection': source}
    raw = json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode()

    source_token = hashlib.sha256(raw).hexdigest()
    if kind == 'turn':
        return 200, {'revision': 7, 'totalCount': total, 'index': index,
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
    return 200, {'revision': 7, 'index': index, 'offset': offset,
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


def test_final_header_revision_change_is_not_reported_as_consistent():
    headers = 0
    def serve(request):
        nonlocal headers
        status, body = pages(request, total=1)
        if 'kind=header' in request.path:
            headers += 1
            if headers == 2:
                body['revision'] = body['document']['metadata']['rev'] = 8
        return status, body
    with api_server(serve) as url:
        with pytest.raises(inspector.DiagnosticAPIError, match='changed'):
            inspector._load_api_document(inspector._DiagnosticAPI(url, None),
                                         'conversation', full=True)


def test_unaffordable_turn_reports_unread_and_resume_without_downloading(monkeypatch, capsys):
    monkeypatch.setattr(inspector._DiagnosticAPI, 'MAX_TOTAL_BYTES', 3 * 1024 * 1024)
    seen = []
    def serve(request):
        seen.append(request.path)
        return pages(request, total=1, projection={'content': 'x' * (3 * 1024 * 1024)})
    with api_server(serve) as url:
        assert inspector.main(['conversation', '--api-url', url, '--no-logs']) == 3
    output = capsys.readouterr().out
    assert 'INCOMPLETE:' in output
    assert '0 read, 1 unread' in output
    assert '--start-index 0 --max-turns 1' in output
    assert not any('kind=chunk' in path for path in seen)
    assert seen[-1].endswith('?kind=header')


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
