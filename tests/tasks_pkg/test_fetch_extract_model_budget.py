"""Fetched-page helpers obey the parent account at real provider ingress."""
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.execution_budget import ExecutionBudget
from lib.evolution.bridge import derive_actuals
from tests.support.loopback import LoopbackThreadingHTTPServer


@pytest.fixture
def extraction_provider(monkeypatch):
    import lib.search_extract as extract
    import lib.token_counter
    from lib.llm.stream import stream_chat
    monkeypatch.setattr(lib.token_counter, 'count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 100000)
    bodies = []
    options = {'usage': True}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            bodies.append(body)
            final = {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}
            if options['usage']:
                final['usage'] = {'prompt_tokens': 20, 'completion_tokens': min(5, body['max_tokens'])}
            payload = (b'data: {"choices":[{"delta":{"content":"source-grounded answer"}}]}\n\n'
                       + b'data: ' + json.dumps(final).encode() + b'\n\n'
                       + b'data: [DONE]\n\n')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    forwarded = []
    def local_dispatch(request, *, on_content=None, abort_check=None, **kwargs):
        forwarded.append(kwargs)
        # Substitute only pool selection and the provider. Real canonical
        # ingress, admission, wire projection, SSE and settlement remain active.
        from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
        slot = SimpleNamespace(model='gpt-4o', provider_id='',
                               routing_provider_id='', protocol='openai',
                               thinking_format='', max_output_tokens=4096)
        body = _adapt_stream_body_for_slot(
            slot, request, isinstance(request, dict), tools=None,
            max_tokens=kwargs['max_tokens'], temperature=kwargs['temperature'],
            thinking_enabled=kwargs['thinking_enabled'], preset='low',
            effort=None)
        assert body['stream'] is True
        return stream_chat(body, on_content=on_content, abort_check=abort_check,
                           base_url=f'http://127.0.0.1:{server.server_port}/v1',
                           owner_user_id=kwargs.get('owner_user_id', 7))
    monkeypatch.setattr(extract, 'dispatch_stream', local_dispatch)
    yield bodies, options, forwarded
    from lib.llm._transport import reset_pools_for_test
    reset_pools_for_test()
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def parent(limit=50, *, deadline=None):
    task = {'id': 'bounded-page-extract', '_userId': 7, '_transientRuntime': True,
            'abort_event': threading.Event(), 'aborted': False, 'usage': {},
            'lastUserQuery': 'source question', '_tool_result_cache': {},
            'config': {'executionLimits': {'max_tokens': limit,
                       'max_tool_calls': None, 'deadline': deadline},
                       'evolution': {'stage': 'executing'}}}
    task['_executionBudget'] = ExecutionBudget(task)
    return task


def fetch(monkeypatch, task):
    from lib.tasks_pkg.handlers.search import _handlers as handlers
    page = 'retained original source'
    item = {'url': 'https://source.test/page', 'page_content': page,
            'fetched_source': page, 'is_pdf': False, 'raw_chars': len(page),
            'filtered_chars': len(page), 'error_msg': None, 'saved_path': None,
            'is_asset': False}
    backend = SimpleNamespace(_fetch_url_one=lambda *a, **k: item)
    monkeypatch.setattr(handlers, '_load_search_backend', lambda: (backend, None))
    monkeypatch.setattr(handlers, '_finalize_tool_round', lambda *a, **k: None)
    monkeypatch.setattr('lib.search_bridge.bind_search_browser', lambda **k: nullcontext())
    entry = {}
    _, content, fetched = handlers._handle_fetch_url(
        task, {}, 'fetch_url', 'extract-call',
        {'url': item['url'], 'extract': 'What does the source establish?'},
        0, entry, task['config'], '', False)
    assert fetched
    return content, entry


@pytest.mark.api
def test_extract_shares_parent_reserves_wire_caps_and_refuses_next_send(monkeypatch, extraction_provider):
    bodies, _, forwarded = extraction_provider
    task = parent()
    first, _ = fetch(monkeypatch, task)
    second, _ = fetch(monkeypatch, task)
    third, entry = fetch(monkeypatch, task)
    assert 'source-grounded answer' in first and 'source-grounded answer' in second
    assert len(bodies) == 2
    assert [body['max_tokens'] for body in bodies] == [30, 5]
    assert all(not any(key.startswith('_') for key in body) for body in bodies)
    assert all(row['owner_user_id'] == 7 for row in forwarded)
    assert derive_actuals(task)['tokens'] == 50
    assert task['_executionBudget'].actuals_complete()
    assert 'retained original source' in third and 'extractMode' not in entry
    assert task['_executionLimitReason'] == 'execution_token_budget_exhausted'


@pytest.mark.api
def test_unknown_extraction_usage_fences_parent_and_keeps_source(monkeypatch, extraction_provider):
    bodies, options, _ = extraction_provider
    options['usage'] = False
    task = parent()
    first, _ = fetch(monkeypatch, task)
    assert 'source-grounded answer' in first
    assert not task['_executionBudget'].actuals_complete()
    assert task['_executionActualsComplete'] is False
    second, entry = fetch(monkeypatch, task)
    assert len(bodies) == 1
    assert 'retained original source' in second and 'extractMode' not in entry
    assert task['_executionLimitReason'] == 'execution_token_usage_unknown'


@pytest.mark.api
@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_parent_stop_prevents_auxiliary_send_and_keeps_source(monkeypatch, extraction_provider, stop):
    import time
    bodies, _, _ = extraction_provider
    task = parent(deadline=time.time() - 1 if stop == 'deadline' else None)
    if stop == 'cancel':
        task['aborted'] = True
        task['abort_event'].set()
    content, entry = fetch(monkeypatch, task)
    assert bodies == []
    assert 'retained original source' in content and 'extractMode' not in entry
    assert task['aborted']
    assert task['_executionBudget'].consumption() == (0, 0)
    assert task['_executionBudget'].actuals_complete()
