"""Direct fetch keeps received source recoverable beyond preview/cache caps."""

from __future__ import annotations

import json

import pytest

pytest_plugins = ['tests._artifact_sidecar']
pytestmark = pytest.mark.unit


def test_direct_fetch_preserves_json_past_both_preview_and_cache_caps(monkeypatch):
    from tofu_search import configure
    import tofu_search.fetch.core as core
    import tofu_search.fetch.utils as utils

    configure(block_private_addresses=False)
    monkeypatch.setattr(utils, '_host_is_safe', lambda host: True)

    class Response:
        headers = {'Content-Type': 'application/json'}
        encoding = 'utf-8'

    body = json.dumps({'padding': 'z' * 470_000,
                       'late_record': {'id': 'far-end-evidence'}},
                      ensure_ascii=False)
    requests = []

    def fake_request(url, timeout, **kwargs):
        requests.append(url)
        return Response(), body.encode('utf-8')

    monkeypatch.setattr(core, '_do_request', fake_request)
    url = 'https://api.example.com/long-catalog.json'
    # Search may first populate its *short* shared cache; a direct source
    # observation must never read that excerpt and call it a complete page.
    short = core.fetch_page_content(url, max_chars=1_000)
    assert short.endswith('[…truncated]')
    full = core.fetch_page_content(url, max_chars=0)
    assert full == body
    assert 'far-end-evidence' in full
    assert requests == [url, url]


def test_extract_scans_source_after_first_48k_without_a_second_model_pass(monkeypatch):
    import lib.search_extract as extract
    # Keep provider verification isolated from lexical retrieval.
    monkeypatch.setattr('lib.llm.stream_result.require_verified_provider_stream_result',
                        lambda stream, **kwargs: stream)
    messages_seen = []

    def fake_dispatch(messages, *, on_content=None, **kwargs):
        messages_seen.append(messages)
        on_content('{"findings":[{"fact":"answer is 9137","quote":"max_completion_tokens 9137"}],"unresolved":[]}')
        return type('Result', (), {'message': {'content': 'answer'}})()

    monkeypatch.setattr(extract, 'dispatch_stream', fake_dispatch)
    source = ('z' * 130_000 + '\n' +
              'late_identifier_xyz has max_completion_tokens 9137\n' +
              'z' * 65_000)
    answer = extract.extract_answer(
        'What is late_identifier_xyz max_completion_tokens?',
        'https://example.test/catalog', source)
    assert 'answer is 9137' in answer
    assert 'Source chars [' in answer
    assert len(messages_seen) == 1
    prompt = messages_seen[0][1]['content']
    assert 'max_completion_tokens 9137' in prompt
    assert 'Selected evidence windows' in prompt
    assert len(prompt) < 50_000


def test_direct_fetch_handler_retains_full_source_and_exposes_tail(monkeypatch):
    from lib.tasks_pkg.handlers.search import _core, _handlers
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery

    body = 'x' * 420_000 + 'UNIQUE_TAIL_ENTRY=9281'
    url = 'https://example.test/catalog.json'
    seen = []
    monkeypatch.setattr(_core, 'fetch_page_content',
                        lambda target, **kw: seen.append(kw['max_chars']) or body)
    monkeypatch.setattr(_core, '_stage_binary_asset', lambda *args, **kw: None)
    fetched = _core._fetch_url_one(url, 'find UNIQUE_TAIL_ENTRY')
    assert seen == [0]
    assert fetched['page_content'] == body
    monkeypatch.setattr(_handlers, '_finalize_tool_round', lambda *args, **kw: None)
    monkeypatch.setattr(_core, '_fetch_url_one', lambda *args, **kw: fetched)
    task = {'id': 'source-test', '_userId': 17, 'lastUserQuery': 'find tail'}
    row = {}
    _, content, _ = _handlers._handle_fetch_url(
        task, {}, 'fetch_url', 'tc-source', {'url': url}, 1,
        row, {}, '', False)
    delivery = split_tool_result_delivery(_budget.budget_tool_result_v2(
        'fetch_url', content, user_id=17))
    assert delivery.evidence['artifactRef']
    hit = ToolResultArtifactRepository().search(
        user_id=17, artifact_ref=delivery.evidence['artifactRef'],
        query='UNIQUE_TAIL_ENTRY')
    assert hit['items']
    assert '9281' in hit['items'][0]['text']


def test_rewrite_retains_original_source_not_only_rewritten_preview(monkeypatch):
    from lib.tasks_pkg.handlers.search import _core, _handlers
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery

    url = 'https://example.test/rewrite'
    item = {'url': url, 'page_content': 'short rewritten answer',
            'fetched_source': 'ORIGINAL_CRITICAL_EVIDENCE=71' * 4000,
            'raw_chars': 116000, 'filtered_chars': 22, 'error_msg': None,
            'is_pdf': False, 'saved_path': None, 'is_asset': False}
    monkeypatch.setattr(_handlers, '_finalize_tool_round', lambda *args, **kw: None)
    monkeypatch.setattr(_core, '_fetch_url_one', lambda *args, **kw: item)
    _, content, _ = _handlers._handle_fetch_url(
        {'id': 'rewrite', '_userId': 17, 'lastUserQuery': ''}, {},
        'fetch_url', 'tc-rw', {'url': url}, 1, {}, {}, '', False)
    assert 'short rewritten answer' in content
    delivery = split_tool_result_delivery(_budget.budget_tool_result_v2(
        'fetch_url', content, user_id=17))
    assert delivery.evidence['artifactRef']
    hits = ToolResultArtifactRepository().search(
        user_id=17, artifact_ref=delivery.evidence['artifactRef'],
        query='ORIGINAL_CRITICAL_EVIDENCE')
    assert hits['items']


def test_extract_answer_keeps_long_source_behind_visible_reference(monkeypatch):
    from lib.tasks_pkg.handlers.search import _core, _handlers
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tools.result_value import TrustedResultText
    import lib.search_extract as extract

    url = 'https://example.test/page'
    body = 'x' * 100_000 + 'LATE_SOURCE_VALUE=347'
    fetched = {'url': url, 'page_content': body, 'raw_chars': len(body),
               'filtered_chars': len(body), 'error_msg': None, 'is_pdf': False,
               'saved_path': None, 'is_asset': False}
    monkeypatch.setattr(_handlers, '_finalize_tool_round', lambda *args, **kw: None)
    monkeypatch.setattr(_core, '_fetch_url_one', lambda *args, **kw: fetched)
    monkeypatch.setattr(extract, 'extract_answer', lambda *args, **kwargs: 'Answer: 347')
    _, content, _ = _handlers._handle_fetch_url(
        {'id': 'extract-test', '_userId': 17, 'lastUserQuery': ''}, {},
        'fetch_url', 'tc-extract', {'url': url, 'extract': 'value?'},
        1, {}, {}, '', False)
    assert isinstance(content, TrustedResultText)
    assert 'LATE_SOURCE_VALUE' not in content
    delivery = split_tool_result_delivery(_budget.budget_tool_result_v2(
        'fetch_url', content, user_id=17))
    assert delivery.evidence['artifactRef']
    assert 'source retained' in delivery.model_text
    hit = ToolResultArtifactRepository().search(
        user_id=17, artifact_ref=delivery.evidence['artifactRef'],
        query='LATE_SOURCE_VALUE')
    assert hit['items']


def test_real_four_megabyte_chunk_boundary_retains_tail():
    from lib.tools.result_value import CapturedToolResult, ResultPart, persist_result
    from lib.tool_result_artifacts import ToolResultArtifactRepository

    source = 'A' * (4 * 1024 * 1024 - 2) + '🙂' + 'TAIL_RECORD=61'
    result = CapturedToolResult((ResultPart('text', source),))
    ref = persist_result(result, user_id=17)
    hits = ToolResultArtifactRepository().search(
        user_id=17, artifact_ref=ref, query='TAIL_RECORD')
    assert hits['items']
    assert hits['items'][0]['path'] == ['/parts/1']


def test_large_result_chunks_preserve_tail_and_unicode_for_artifact_queries(monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery
    import lib.tools.result_value as result_value

    monkeypatch.setattr(result_value, '_ARTIFACT_TEXT_CHUNK_BYTES', 2048)
    body = '首段🙂' + 'x' * 190_000 + '远处标记_abc🙂' + 'y' * 15_000
    result = _budget.budget_tool_result_v2('fetch_url', body, user_id=17)
    evidence = split_tool_result_delivery(result).evidence
    assert evidence['artifactRef']
    repo = ToolResultArtifactRepository()
    hits = repo.search(user_id=17, artifact_ref=evidence['artifactRef'],
                       query='远处标记_abc')
    assert hits['items'], hits
    assert hits['retentionCoverage'] == 'complete'
    cursor, pages = '0', []
    for _ in range(100):
        page = repo.read_range(user_id=17, artifact_ref=evidence['artifactRef'],
                               offset=cursor, limit=8192)
        pages.append(page['content'])
        cursor = page.get('nextCursor')
        if not cursor:
            break
    else:
        pytest.fail('artifact continuation failed to terminate')
    assert ''.join(pages) == body



def test_search_unknown_candidates_survive_preview_omission_in_original_artifact(monkeypatch):
    from lib.tasks_pkg.handlers.search import _core, _handlers
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery
    from tofu_search.search.orchestrator import SearchResultList

    results = SearchResultList()
    results._unverified_candidates = [
        {'url': f'https://unknown.test/CANDIDATE_{i}', 'title': f'Unknown {i}',
         'snippet': 'Unverified engine observation', 'source': 'test'} for i in range(10)]
    results._search_diag = {'reason': 'no_usable_evidence', 'unverified_count': 10,
        'unverified_candidates': results._unverified_candidates[:3]}
    monkeypatch.setattr(_core, '_web_search_one', lambda *a, **kw: (results, results._search_diag, None, None))
    monkeypatch.setattr(_handlers, 'append_event', lambda *a, **kw: None)
    _, content, _ = _handlers._handle_web_search(
        {'id': 'unknown-search', '_userId': 17, 'lastUserQuery': 'quasar'}, {},
        'web_search', 'tc-search', {'query': 'quasar'}, 1, {}, {}, '', False)
    assert 'CANDIDATE_9' not in str(content)
    delivery = split_tool_result_delivery(_budget.budget_tool_result_v2('web_search', content, user_id=17))
    assert delivery.evidence['sourceCoverage'] == 'unknown'
    hit = ToolResultArtifactRepository().search(user_id=17,
        artifact_ref=delivery.evidence['artifactRef'], query='CANDIDATE_9')
    assert hit['items']
