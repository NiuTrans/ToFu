"""The Sources footer cites only pages this turn actually opened.

Evidence is the turn's settled page-reading rounds: ``fetch_url`` rows marked
``fetched`` (fresh, streaming-prefetched or dedup-replayed) and successful
``browser_read_page`` reads that carry the accepted tab URL. A search listing
names every engine hit, and an opened page links to other pages; neither is a
page the model opened. The real ``run_task`` cases fail on the retired
text-scraping footer, which listed search hits and page-body links while
missing pages served by the streaming prefetch.
"""
from __future__ import annotations

import lib.tasks_pkg.llm_fallback._transport as _fallback_transport

import json

import pytest

from lib.tasks_pkg.orchestrator._finalize import _maybe_append_sources_footer

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

FOOTER_HEADING = '**来源 / Sources**'
GUIDE = 'https://docs.example.test/guide'
BODY_LINK = 'https://ads.example.test/banner'
SEARCH_HITS = ('https://m.imdb.com/title/tt0732097',
               'https://m.imdb.com/title/tt0732130',
               'https://adult-spam.example.test/landing')


def _task(content, rounds, *, aborted=False):
    return {'id': 'footer-task', 'content': content, 'aborted': aborted,
            'toolRounds': rounds}


def _fetch_round(*rows, status='done', **extra):
    return {'toolName': 'fetch_url', 'status': status, 'results': list(rows), **extra}


def _fetched(url, fetched=True):
    return {'title': 'Page', 'url': url, 'source': 'Direct Fetch',
            'fetched': fetched, 'fetchedChars': 120 if fetched else 0}


def _search_round():
    rows = [{'title': f'hit {i}', 'url': url, 'source': 'DuckDuckGo',
             'fetched': True, 'fetchedChars': 900}
            for i, url in enumerate(SEARCH_HITS)]
    return {'toolName': 'web_search', 'status': 'done', 'results': rows}


def _footer_urls(content):
    if FOOTER_HEADING not in content:
        return []
    footer = content.split(FOOTER_HEADING, 1)[1]
    return [line[2:] for line in footer.splitlines() if line.startswith('- ')]


class TestOpenedPageEvidence:
    def test_uncited_opened_page_is_listed(self):
        task = _task('The guide explains setup.', [_fetch_round(_fetched(GUIDE))])
        _maybe_append_sources_footer(task)
        assert task['content'].startswith('The guide explains setup.\n\n---\n')
        assert _footer_urls(task['content']) == [GUIDE]

    def test_search_hits_are_candidates_not_sources(self):
        # Engine rows carry fetched=True when the engine excerpted the page;
        # the model still never chose to open any of them.
        task = _task('Answer without links.', [_search_round()])
        _maybe_append_sources_footer(task)
        assert task['content'] == 'Answer without links.'

    def test_only_the_opened_page_not_search_hits_beside_it(self):
        task = _task('Answer.', [_search_round(), _fetch_round(_fetched(GUIDE))])
        _maybe_append_sources_footer(task)
        assert _footer_urls(task['content']) == [GUIDE]

    @pytest.mark.parametrize('round_entry', [
        _fetch_round(_fetched(GUIDE, fetched=False)),
        _fetch_round(_fetched(GUIDE), status='error'),
        _fetch_round(_fetched(GUIDE), status='rejected'),
        _fetch_round(_fetched(GUIDE), status='searching'),
        {'toolName': 'read_tool_artifact', 'status': 'done', 'results': [_fetched(GUIDE)]},
    ])
    def test_unsettled_failed_or_non_reading_rounds_are_not_openings(self, round_entry):
        task = _task('Answer.', [round_entry])
        _maybe_append_sources_footer(task)
        assert task['content'] == 'Answer.'

    def test_prefetched_and_dedup_replayed_rows_are_openings(self):
        rounds = [_fetch_round(_fetched(GUIDE), cacheSource='prefetch'),
                  _fetch_round(_fetched(GUIDE), cacheSource='cache')]
        task = _task('Answer.', rounds)
        _maybe_append_sources_footer(task)
        assert _footer_urls(task['content']) == [GUIDE]

    @pytest.mark.parametrize('cited', [False, True])
    def test_fragments_dedupe_and_a_cited_page_suppresses_the_footer(self, cited):
        page = 'https://example.test/page'
        content = f'answer {page}' if cited else 'answer'
        task = _task(content, [_fetch_round(_fetched(page + '#part'), _fetched(page))])
        _maybe_append_sources_footer(task)
        assert task['content'].count(page) == 1
        assert (FOOTER_HEADING in task['content']) is not cited

    def test_footer_keeps_first_open_order_and_caps_at_five(self):
        pages = [f'https://ex{i}.example.test/x' for i in range(9)]
        task = _task('no links', [_fetch_round(*(_fetched(url) for url in pages))])
        _maybe_append_sources_footer(task)
        assert _footer_urls(task['content']) == pages[:5]

    @pytest.mark.parametrize('content,aborted', [('', False), ('  ', False), ('partial', True)])
    def test_empty_or_aborted_answer_gets_no_footer(self, content, aborted):
        task = _task(content, [_fetch_round(_fetched(GUIDE))], aborted=aborted)
        _maybe_append_sources_footer(task)
        assert task['content'] == content

    def test_browser_page_read_is_listed_by_its_accepted_url(self):
        rounds = [
            {'toolName': 'browser_read_page', 'status': 'done',
             'results': [{'title': 'browser_read_page', 'source': 'Browser',
                          'fetched': True, 'url': 'https://tab.example.test/doc'}]},
            {'toolName': 'browser_click', 'status': 'done',
             'results': [{'title': 'browser_click', 'source': 'Browser',
                          'fetched': True, 'url': ''}]},
        ]
        task = _task('Answer.', rounds)
        _maybe_append_sources_footer(task)
        assert _footer_urls(task['content']) == ['https://tab.example.test/doc']

    def test_opened_urls_cannot_inject_markdown_into_the_answer(self):
        rounds = [_fetch_round(
            _fetched('https://evil.example.test/a\n# Injected heading'),
            _fetched('javascript:alert(1)'),
            _fetched('https://evil.example.test/a)![x](https://track.example.test/p.png'),
            _fetched('https://en.wikipedia.org/wiki/Tofu_(food)'))]
        task = _task('Answer.', rounds)
        _maybe_append_sources_footer(task)
        assert _footer_urls(task['content']) == [
            'https://evil.example.test/a%29!%5Bx%5D%28https://track.example.test/p.png',
            'https://en.wikipedia.org/wiki/Tofu_%28food%29']
        assert 'Injected heading' not in task['content']
        assert '](' not in task['content']


# ── Real run_task: the classic finalize applies the footer ─────────────────


def _seed_conv(conv_id):
    from tests._seed import seed_conversation
    seed_conversation(conv_id, title='sources-footer')


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
        conv_id, command_id=f'sources-footer:{conv_id}',
        input_projection={'content': 'look this up'}, config=config, user_id=1)
    config.update({'_turnId': created['turn']['turnId'],
                   '_attemptId': created['attempt']['attemptId']})
    assert claim_attempt_start(created['attempt']['attemptId'], user_id=1)
    task = create_task(conv_id, [{'role': 'user', 'content': 'look this up'}], config, user_id=1)
    bind_task(created['attempt']['attemptId'], task['id'], user_id=1)
    started = mark_task_started(created['attempt']['attemptId'], task['id'], user_id=1)
    assert started and started.get('status') == 'running'
    return task


def _install_turn(monkeypatch, tool_calls, *, prefetch):
    """Round 0 issues *tool_calls*; round 1 answers without citing a URL."""
    from lib.agent_core.events import EventType, build_event
    import lib.tasks_pkg.handlers.search._core as search_core
    import lib.tasks_pkg.llm_fallback._call as llm_call
    from lib.tasks_pkg.manager._events import append_event
    import tofu_search

    answer = 'The guide explains the setup steps.'

    def stream(task, body, tag='', on_tool_call_ready=None):
        if not task.get('_footer_tools_done'):
            task['_footer_tools_done'] = True
            calls = [{'id': f'call_footer_{index}', 'index': index, 'type': 'function',
                      'function': {'name': name, 'arguments': json.dumps(args)}}
                     for index, (name, args) in enumerate(tool_calls)]
            if prefetch and on_tool_call_ready:
                for call in calls:
                    on_tool_call_ready(call)
            return ({'role': 'assistant', 'content': '', 'tool_calls': calls}, 'tool_calls',
                    {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
        with task['content_lock']:
            task['content'] += answer
        append_event(task, build_event(EventType.DELTA, content=answer))
        return ({'role': 'assistant', 'content': answer, 'tool_calls': []}, 'stop',
                {'prompt_tokens': 20, 'completion_tokens': 9, 'total_tokens': 29})

    def search(query, user_question='', freshness='', **kwargs):
        return [{'title': f'Unrelated hit {index}', 'snippet': 'listing only',
                 'url': url, 'source': 'DuckDuckGo'}
                for index, url in enumerate(SEARCH_HITS)]

    def fetch(url, user_question='', fetch_reason='', **kwargs):
        body = f'Setup guide. Sponsored: {BODY_LINK} and https://ads.example.test/two'
        return {'url': url, 'page_content': body, 'is_pdf': False,
                'raw_chars': len(body), 'filtered_chars': len(body), 'error_msg': None}

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    monkeypatch.setattr(tofu_search, 'perform_web_search', search)
    monkeypatch.setattr(search_core, 'perform_web_search', search)
    monkeypatch.setattr(search_core, '_fetch_url_one', fetch)
    return answer


def _run_turn(monkeypatch, conv_id, tool_calls, *, prefetch):
    from lib.tasks_pkg.orchestrator.api import run_task
    from tests._seed import conv_document
    _cleanup_conv(conv_id)
    _seed_conv(conv_id)
    answer = _install_turn(monkeypatch, tool_calls, prefetch=prefetch)
    try:
        task = _create_task(conv_id)
        run_task(task)
        assistant = [message for message in conv_document(conv_id)['messages']
                     if message.get('role') == 'assistant'][-1]
        return answer, task['content'], assistant.get('content'), assistant.get('toolRounds') or []
    finally:
        _cleanup_conv(conv_id)


# Same resource as test_segment_model's real run_task class: under the CI
# parallel lane the shared storage pool's writes contend ('database is locked').
@pytest.mark.serial
class TestRealTurnFooter:
    def test_search_only_turn_has_no_footer(self, monkeypatch, chat_sidecar):
        answer, content, durable, rounds = _run_turn(
            monkeypatch, 'cv-footer-search-only',
            [('web_search', {'query': 'no available channel 503', 'vertical': 'off'})], prefetch=False)
        assert [row['toolName'] for row in rounds] == ['web_search']
        assert content == durable == answer
        assert not any(hit in content for hit in SEARCH_HITS)

    @pytest.mark.parametrize('prefetch', [False, True])
    def test_opened_page_is_listed_but_not_search_hits_or_its_links(
            self, monkeypatch, chat_sidecar, prefetch):
        answer, content, durable, rounds = _run_turn(
            monkeypatch, f'cv-footer-opened-{int(prefetch)}',
            [('web_search', {'query': 'setup guide', 'vertical': 'off'}), ('fetch_url', {'url': GUIDE})],
            prefetch=prefetch)
        fetch_round = next(row for row in rounds if row['toolName'] == 'fetch_url')
        assert fetch_round['status'] == 'done'
        assert (fetch_round.get('cacheSource') == 'prefetch') is prefetch
        assert durable == content
        assert content.startswith(answer + '\n\n---\n')
        assert _footer_urls(content) == [GUIDE]
        assert BODY_LINK not in content
        assert not any(hit in content for hit in SEARCH_HITS)
