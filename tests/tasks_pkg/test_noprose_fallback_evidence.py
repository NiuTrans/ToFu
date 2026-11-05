"""The no-prose fallback answers from every page the turn settled, once.

When a turn gathered web evidence but its last model round wrote no prose, the
post-loop fallback asks the model to answer from that evidence. The evidence is
each ``web_search``/``fetch_url``/page-delivering ``browser_read_page`` result
exactly as it settled for the model, recorded once per distinct result after
the round's aggregate budget. A page therefore reaches the fallback identically
whether the handler fetched it, the streaming prefetch served it, a dedup
replay repeated it or the native gateway executed it; the Sources footer never
names a page the fallback lacked. One pipeline owner admits evidence: another
tool's receipt, a failed result and a read that delivered no page never are.
"""
from __future__ import annotations

import lib.tasks_pkg.llm_fallback._transport as _fallback_transport

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

GUIDE = 'https://docs.example.test/guide'
PAGE_MARKER = 'NOPROSE-GUIDE-BODY'
LISTING_MARKER = 'NOPROSE-LISTING-SNIPPET'
FALLBACK_ANSWER = 'Synthesized from the gathered evidence.'
EVIDENCE_HEADER = 'Here are fetched contents:'
FOOTER_HEADING = '**来源 / Sources**'
SEARCH = ('web_search', {'query': 'setup guide', 'vertical': 'off'})
FETCH = ('fetch_url', {'url': GUIDE})


def _page_body(size):
    body = f'{PAGE_MARKER}: install, configure, verify.'
    return body + ' Setup step detail.' * max(0, (size - len(body)) // 19)


def _seed_conv(conv_id):
    from tests._seed import seed_conversation
    seed_conversation(conv_id, title='noprose-fallback')


def _cleanup_conv(conv_id):
    from tests._seed import delete_conversation
    try:
        delete_conversation(conv_id)
    except Exception:
        pass


def _create_task(conv_id):
    from lib.tasks_pkg.manager import create_task
    from lib.turn_lifecycle import (bind_task, claim_attempt_start, create_turn_pair,
                                    mark_task_started)
    config = {'model': 'test-model', 'projectEnabled': False, 'webSearchEnabled': True}
    created = create_turn_pair(
        conv_id, command_id=f'noprose-fallback:{conv_id}',
        input_projection={'content': 'look this up'}, config=config, user_id=1)
    config.update({'_turnId': created['turn']['turnId'],
                   '_attemptId': created['attempt']['attemptId']})
    assert claim_attempt_start(created['attempt']['attemptId'], user_id=1)
    task = create_task(conv_id, [{'role': 'user', 'content': 'look this up'}], config, user_id=1)
    bind_task(created['attempt']['attemptId'], task['id'], user_id=1)
    started = mark_task_started(created['attempt']['attemptId'], task['id'], user_id=1)
    assert started and started.get('status') == 'running'
    return task


def _install_turn(monkeypatch, tool_rounds, *, prefetch, page_size):
    """Each model round issues one entry of *tool_rounds*; the next ends without prose."""
    from lib.agent_core.events import EventType, build_event
    import lib.tasks_pkg.handlers.search._core as search_core
    import lib.tasks_pkg.llm_fallback._call as llm_call
    import lib.tasks_pkg.orchestrator._finalize as finalize
    from lib.tasks_pkg.manager._events import append_event
    import tofu_search

    usage = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}
    fallback_bodies = []

    def stream(task, body, tag='', on_tool_call_ready=None):
        step = task.get('_noprose_step', 0)
        if step >= len(tool_rounds):
            return {'role': 'assistant', 'content': '', 'tool_calls': []}, 'stop', usage
        task['_noprose_step'] = step + 1
        calls = [{'id': f'call_noprose_{step}_{index}', 'index': index, 'type': 'function',
                  'function': {'name': name, 'arguments': json.dumps(args)}}
                 for index, (name, args) in enumerate(tool_rounds[step])]
        if prefetch and on_tool_call_ready:
            for call in calls:
                on_tool_call_ready(call)
        return ({'role': 'assistant', 'content': '', 'tool_calls': calls}, 'tool_calls', usage)

    def fallback_stream(task, body, tag='', **_kwargs):
        assert tag == 'FALLBACK'
        fallback_bodies.append(body)
        with task['content_lock']:
            task['content'] += FALLBACK_ANSWER
        append_event(task, build_event(EventType.DELTA, content=FALLBACK_ANSWER))
        return ({'role': 'assistant', 'content': FALLBACK_ANSWER, 'tool_calls': []}, 'stop', usage)

    def search(query, user_question='', freshness='', **kwargs):
        return [{'title': 'Setup guide', 'snippet': LISTING_MARKER, 'url': GUIDE,
                 'source': 'DuckDuckGo'}]

    def fetch(url, user_question='', fetch_reason='', **kwargs):
        body = _page_body(page_size)
        return {'url': url, 'page_content': body, 'is_pdf': False,
                'raw_chars': len(body), 'filtered_chars': len(body), 'error_msg': None}

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    monkeypatch.setattr(finalize, 'stream_llm_response', fallback_stream)
    monkeypatch.setattr(tofu_search, 'perform_web_search', search)
    monkeypatch.setattr(search_core, 'perform_web_search', search)
    monkeypatch.setattr(search_core, '_fetch_url_one', fetch)
    return fallback_bodies


def _evidence_text(body):
    """The fallback_synthesis injection that carries the gathered evidence."""
    texts = []
    for message in body['messages']:
        content = message.get('content')
        parts = content if isinstance(content, list) else [{'text': content}]
        texts.extend(part.get('text') for part in parts
                     if isinstance(part, dict) and isinstance(part.get('text'), str))
    matches = [text for text in texts if EVIDENCE_HEADER in text]
    assert len(matches) == 1, 'fallback request must carry one evidence injection'
    return matches[0]


def _footer_urls(content):
    if FOOTER_HEADING not in content:
        return []
    footer = content.split(FOOTER_HEADING, 1)[1]
    return [line[2:] for line in footer.splitlines() if line.startswith('- ')]


def _run_turn(monkeypatch, conv_id, tool_rounds, *, prefetch, page_size=400):
    """Run one real turn; return (final content, durable rounds, fallback bodies)."""
    from lib.tasks_pkg.orchestrator.api import run_task
    from tests._seed import conv_document
    _cleanup_conv(conv_id)
    _seed_conv(conv_id)
    fallback_bodies = _install_turn(monkeypatch, tool_rounds, prefetch=prefetch,
                                    page_size=page_size)
    try:
        task = _create_task(conv_id)
        run_task(task)
        assistant = [message for message in conv_document(conv_id)['messages']
                     if message.get('role') == 'assistant'][-1]
        return task['content'], assistant.get('toolRounds') or [], fallback_bodies
    finally:
        _cleanup_conv(conv_id)


def _settled(rounds, tool_name):
    rows = [row for row in rounds if row.get('toolName') == tool_name]
    assert rows and all(row.get('status') == 'done' for row in rows)
    return rows


# Same resource as the footer's real run_task class: the shared storage pool's
# writes contend under the parallel lane ('database is locked').
@pytest.mark.serial
class TestRealTurnFallbackEvidence:
    @pytest.mark.parametrize('prefetch', [False, True])
    def test_opened_page_and_listing_reach_the_fallback_once(
            self, monkeypatch, chat_sidecar, prefetch):
        content, rounds, bodies = _run_turn(
            monkeypatch, f'cv-noprose-opened-{int(prefetch)}', [[SEARCH, FETCH]],
            prefetch=prefetch)
        fetch_row, = _settled(rounds, 'fetch_url')
        assert (fetch_row.get('cacheSource') == 'prefetch') is prefetch
        assert len(bodies) == 1
        evidence = _evidence_text(bodies[0])
        assert evidence.count(PAGE_MARKER) == 1
        assert evidence.count(LISTING_MARKER) == 1
        assert fetch_row['toolContent'] in evidence
        # The footer appended to the fallback answer names only pages whose
        # content the fallback was given.
        assert content.startswith(FALLBACK_ANSWER)
        assert _footer_urls(content) == [GUIDE]

    def test_prefetch_only_turn_still_synthesizes_from_its_page(
            self, monkeypatch, chat_sidecar):
        content, rounds, bodies = _run_turn(
            monkeypatch, 'cv-noprose-prefetch-only', [[FETCH]], prefetch=True)
        fetch_row, = _settled(rounds, 'fetch_url')
        assert fetch_row.get('cacheSource') == 'prefetch'
        assert len(bodies) == 1
        assert _evidence_text(bodies[0]).count(PAGE_MARKER) == 1
        assert content.startswith(FALLBACK_ANSWER)

    @pytest.mark.parametrize('prefetch', [False, True])
    @pytest.mark.parametrize('page_size', [400, 120_000])
    def test_dedup_replayed_page_counts_once(
            self, monkeypatch, chat_sidecar, prefetch, page_size):
        _content, rounds, bodies = _run_turn(
            monkeypatch, f'cv-noprose-replay-{int(prefetch)}-{page_size}',
            [[FETCH], [FETCH]], prefetch=prefetch, page_size=page_size)
        first, replay = _settled(rounds, 'fetch_url')
        assert replay.get('cacheSource') in {'cache', 'prefetch'}
        assert replay['toolContent'] == first['toolContent']
        assert len(bodies) == 1
        evidence = _evidence_text(bodies[0])
        assert evidence.count(first['toolContent']) == 1
        assert evidence.count(PAGE_MARKER) == 1

    def test_turn_without_web_evidence_gets_no_fallback(
            self, monkeypatch, chat_sidecar):
        content, _rounds, bodies = _run_turn(
            monkeypatch, 'cv-noprose-no-evidence', [], prefetch=False)
        assert bodies == []
        assert content == ''


# ── Native gateway: the same canonical pipeline fills the borrowed list ─────


def _principal():
    from lib.identity import PrincipalContext
    return PrincipalContext.user(subject_id='noprose-native-gateway', owner_user_id=1)


def _native_task():
    return dict(id='noprose-native', convId='', _userId=1, status='running', aborted=False,
                model='fixture', config={}, events=[], events_lock=threading.Lock(),
                abort_event=threading.Event(), _attended=True, _dispatch_heartbeat=0.0,
                _t_last_event=0.0, toolRounds=[], created_at=time.time())


def test_native_gateway_records_each_settled_page_once(monkeypatch):
    import lib.tasks_pkg.handlers.search._core as search_core
    from lib.agent_core.native_process import ToolRequest
    from lib.tasks_pkg.native_gateway import NativeGateway

    def fetch(url, user_question='', fetch_reason='', **kwargs):
        body = _page_body(400)
        return {'url': url, 'page_content': body, 'is_pdf': False,
                'raw_chars': len(body), 'filtered_chars': len(body), 'error_msg': None}

    monkeypatch.setattr(search_core, '_fetch_url_one', fetch)
    owner = _native_task()
    definitions = [{'type': 'function', 'function': {
        'name': 'fetch_url', 'description': 'Fetch a page',
        'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}},
                       'required': ['url']}}}]
    owner['_executable_tool_catalog'] = definitions
    evidence = []

    async def run(executor):
        gateway = NativeGateway(
            principal=_principal(), task=owner, executor=executor, configuration={},
            tool_list=definitions, permissions={'fetch_url': 'read'}, project_path=None,
            project_enabled=False, model='fixture', round_source=None, decision_policy=None,
            pending_calls=4, pending_bytes=65536, search_results=evidence)
        try:
            for round_num in (0, 1):
                request = ToolRequest(_principal(), owner['id'], f'fetch-{round_num}',
                                      'fetch_url', {'url': GUIDE}, model_round=round_num)
                assert PAGE_MARKER in await gateway.execute(request)
        finally:
            gateway.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        asyncio.run(run(executor))
    first, replay = _settled(owner['toolRounds'], 'fetch_url')
    assert replay.get('cacheSource') == 'cache'
    assert evidence == [first['toolContent']]


# ── Canonical pipeline: one owner admits evidence ───────────────────────────


def _dispatch_round(task, calls, evidence):
    """Dispatch one model round of ``(name, arguments)`` calls; return its rows."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    parsed, rows = [], []
    for index, (name, arguments) in enumerate(calls):
        call_id, serialized = f'evidence-{index}', json.dumps(arguments)
        _, row, _ = _build_tool_round_entry(name, arguments, call_id, serialized, index + 1, False)
        row['llmRound'] = 0
        rows.append(row)
        call = {'id': call_id, 'type': 'function',
                'function': {'name': name, 'arguments': serialized}}
        parsed.append((call, name, call_id, arguments, row['roundNum'], row, None))
    task['toolRounds'] = rows
    messages = []
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=evidence,
        round_num=0, model='test-model')
    return rows, messages


def _page_fetch(monkeypatch, *, failure=None):
    import lib.tasks_pkg.handlers.search._core as search_core

    def fetch(url, user_question='', fetch_reason='', **kwargs):
        body = '' if failure else f'{PAGE_MARKER} {url} ' + 'Setup step detail. ' * 40
        return {'url': url, 'page_content': body, 'is_pdf': False, 'raw_chars': len(body),
                'filtered_chars': len(body), 'error_msg': failure}

    monkeypatch.setattr(search_core, '_fetch_url_one', fetch)


def test_another_tools_declared_evidence_is_never_admitted(observations):
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round

    def staged_receipt(task, tc, name, call_id, arguments, number, row, *rest):
        _finalize_tool_round(task, number, row, [{'title': 'staged', 'url': '', 'fetched': True}])
        return call_id, 'File downloaded to server staging: /staging/guide.pdf', True

    task = task_for(staged_receipt, name='browser_download_url_to_server')
    evidence = []
    (row,), _ = _dispatch_round(task, [('browser_download_url_to_server', {'url': GUIDE})], evidence)
    assert row['status'] == 'done'
    assert evidence == []


def test_successful_search_settings_update_is_not_evidence(monkeypatch, observations):
    from lib.search import search_settings
    from lib.tasks_pkg.handlers.search._settings import _handle_update_search_settings

    monkeypatch.setattr(search_settings, 'apply_updates', lambda args: {
        'ok': True, 'applied': {'block_domain': ['spam.example']}, 'errors': {},
        'effective': {'skip_domains': ['spam.example']}})
    task = task_for(_handle_update_search_settings, name='update_search_settings')
    evidence = []
    (row,), messages = _dispatch_round(
        task, [('update_search_settings', {'block_domain': 'spam.example'})], evidence)
    assert (row['status'] == 'done'
            and 'block_domain=spam.example' in messages[-1]['content'])
    assert evidence == []


@pytest.mark.parametrize('outcome', ['delivered', 'blocked', 'disconnected'])
def test_browser_read_is_evidence_only_when_it_delivered_a_page(
        monkeypatch, observations, outcome):
    import lib.browser.dispatch as browser_dispatch
    from lib.tasks_pkg.handlers.browser import _handle_browser_tool
    from lib.tools.result_envelope import typed_tool_error

    def read(fn_name, fn_args, *, display_metadata=None, **kwargs):
        if outcome == 'disconnected':
            return typed_tool_error(
                'browser_extension_disconnected', retryable=True,
                message='No browser extension is connected for this user at execution time.',
                next_action='Report this connection blocker.').to_envelope_text()
        if outcome == 'blocked':
            return f'Browser content: blocked\nTitle: Sign in\nURL: {GUIDE}\n'
        # The browser read owner names only an accepted page on the row.
        display_metadata['url'] = GUIDE
        return f'Browser content: ok\nTitle: Setup guide\nURL: {GUIDE}\n\n{PAGE_MARKER}'

    monkeypatch.setattr(browser_dispatch, 'execute_browser_tool', read)
    task = task_for(_handle_browser_tool, name='browser_read_page')
    evidence = []
    (row,), _ = _dispatch_round(task, [('browser_read_page', {'tabId': 7})], evidence)
    assert row['status'] == ('error' if outcome == 'disconnected' else 'done')
    if outcome == 'delivered':
        assert evidence == [row['toolContent']] and PAGE_MARKER in evidence[0]
    else:
        assert evidence == []


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('arguments', [{'url': GUIDE}, {'urls': [GUIDE, GUIDE + '/next']}])
def test_failed_fetch_is_not_evidence(monkeypatch, observations, mode, arguments):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url

    _page_fetch(monkeypatch, failure='Connection refused')
    task = task_for(_handle_fetch_url, mode)
    evidence = []
    (row,), messages = _dispatch_round(task, [('fetch_url', arguments)], evidence)
    assert row['status'] == 'error' and 'Connection refused' in messages[-1]['content']
    assert evidence == []


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
def test_evidence_is_the_model_view_after_round_compaction(monkeypatch, observations, mode):
    from lib.tasks_pkg.handlers.search._handlers import _handle_fetch_url
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    compacted = '[round budget: page persisted, re-fetch to read it]'

    def compact_first(results, **_kwargs):
        first = next(iter(results))
        _content, name, call_id = results[first]
        return {**results, first: (compacted, name, call_id)}

    monkeypatch.setattr(pipeline, 'enforce_round_aggregate_budget', compact_first)
    monkeypatch.setattr(pipeline, 'enforce_round_aggregate_budget_v2', compact_first)
    _page_fetch(monkeypatch)
    task = task_for(_handle_fetch_url, mode)
    evidence = []
    rows, messages = _dispatch_round(
        task, [FETCH, ('fetch_url', {'url': GUIDE + '/next'})], evidence)
    assert [row['status'] for row in rows] == ['done', 'done']
    assert rows[0]['toolContent'] == compacted
    assert evidence == [message['content'] for message in messages]
    assert evidence == [row['toolContent'] for row in rows]
