"""Search and delivered page links share chronological discovery authority."""
import asyncio
from copy import deepcopy
from importlib import metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluations.search_quality.contracts import PAGE_CHARS, URL_CHARS, observed_sources, score, tool_schemas
from evaluations.search_quality.runner import execute_tool, manifest, run_one

pytestmark = pytest.mark.unit
ROOT = 'https://index.example/start'
CHAPTER = 'https://archive.example/chapter'
LEAF = 'https://archive.example/record_(final)?item=a%2Fb'
QUOTE = 'The measured delay is 17 milliseconds.'
TASK = {'id': 'observed-navigation', 'region': 'us-en', 'language': 'en',
        'question': 'Find the measured delay.', 'reference_url': LEAF,
        'hosts': ['archive.example'],
        'facts': {'delay': {'answer': '17', 'quote': ['17', 'milliseconds']}}}
ANSWER = json.dumps({'delay': {'value': '17', 'url': LEAF, 'quote': QUOTE}})


def call(name, output, **changes):
    return {'name': 'custom__'+name, 'accepted': True, 'is_error': False,
            'output': output, **changes}


def chain():
    return [call('search', {'results': [{'url': ROOT}]}),
            call('fetch', {'url': ROOT, 'text': f'[Chapter]({CHAPTER})'}),
            call('fetch', {'url': CHAPTER, 'text': f'[原始记录]({LEAF})'})]


@pytest.mark.parametrize('arm', ['tofu', 'ddgs'])
def test_delivered_links_allow_multiple_hops_with_the_same_fetch_budget(arm):
    dispatched = []
    worker = SimpleNamespace(call=lambda *args, **kwargs: dispatched.append((args, kwargs)) or
                             {'url': LEAF, 'text': QUOTE})
    args = {'url': LEAF, 'query': '测量延迟'}
    result = execute_tool(worker, arm, TASK, 'custom__fetch', args, chain(), 7)
    assert result == {'url': LEAF, 'text': QUOTE}
    assert dispatched == [((arm, 'fetch', args), {'region': 'us-en', 'seconds': 7})]
    graded = score(TASK, ANSWER, chain()+[call('fetch', result)])
    assert graded['passed']
    assert graded['retrieval'] == {'primary_url_found': True, 'primary_page_fetched': True,
                                  'reference_term_coverage': {'delay': True}}


@pytest.mark.parametrize('text', [
    f'[Record]({LEAF})', f'<{LEAF}>',
    f'[Record][source]\n\n[source]: {LEAF}',
    f'The record is at\n{LEAF}\n',
])
def test_observed_markdown_targets_keep_parentheses_queries_and_exact_identity(text):
    calls = [chain()[0], call('fetch', {'url': ROOT, 'text': text})]
    worker = SimpleNamespace(call=lambda *a, **k: {'url': LEAF, 'text': QUOTE})
    assert execute_tool(worker, 'tofu', TASK, 'custom__fetch', {'url': LEAF}, calls, 7)['text'] == QUOTE
    # A fragment may select a location in an observed document; truncating a
    # balanced path or decoding an encoded query invents a different identity.
    assert execute_tool(worker, 'tofu', TASK, 'custom__fetch', {'url': LEAF+'#section'}, calls, 7)['text'] == QUOTE
    for unseen in (LEAF.replace('_(final)', '_'), LEAF.replace('%2F', '/')):
        assert execute_tool(None, 'tofu', TASK, 'custom__fetch', {'url': unseen}, calls, 7)['error']


@pytest.mark.parametrize('fault', ['unaccepted', 'callback_error', 'output_error',
                                  'undiscovered_parent', 'empty_text', 'hidden_text',
                                  'oversized_text', 'future_discovery'])
def test_failed_or_unobserved_parent_never_grants_fetch_or_quote_credit(fault):
    calls = [chain()[0], call('fetch', {'url': ROOT, 'text': f'[Record]({LEAF})'})]
    if fault == 'unaccepted':
        calls[1]['accepted'] = False
    elif fault == 'callback_error':
        calls[1]['is_error'] = True
    elif fault == 'output_error':
        calls[1]['output']['error'] = 'fetch_unavailable'
    elif fault == 'undiscovered_parent':
        calls[1]['output']['url'] = 'https://unknown.example/page'
    elif fault == 'empty_text':
        calls[1]['output']['text'] = ''
    elif fault == 'hidden_text':
        calls[1]['output']['full_text'] = calls[1]['output']['text']
        calls[1]['output']['text'] = 'The delivered excerpt contains no link.'
    elif fault == 'oversized_text':
        calls[1]['output']['text'] = 'x' * PAGE_CHARS + calls[1]['output']['text']
    else:
        calls.reverse()
    assert execute_tool(None, 'tofu', TASK, 'custom__fetch', {'url': LEAF}, calls, 7)['error']
    assert not score(TASK, ANSWER, calls+[call('fetch', {'url': LEAF, 'text': QUOTE})])['passed']


def test_failed_search_cannot_seed_navigation_even_with_successful_looking_rows():
    calls = chain()
    calls[0]['output']['error'] = 'search_failed'
    assert execute_tool(None, 'tofu', TASK, 'custom__fetch', {'url': ROOT}, calls, 7)['error']
    assert not score(TASK, ANSWER, calls+[call('fetch', {'url': LEAF, 'text': QUOTE})])['passed']


@pytest.mark.parametrize('text', [
    f'`{LEAF}`', f'```text\n{LEAF}\n```',
    '[Relative](/record)', '[Credentials](https://user:password@archive.example/record)',
    '[Unsupported](file:///etc/hosts)',
    f'[Too long](https://archive.example/{"a" * URL_CHARS})',
])
def test_non_navigation_or_invalid_targets_do_not_authorize_a_guessed_page(text):
    calls = [chain()[0], call('fetch', {'url': ROOT, 'text': text})]
    assert observed_sources(calls)[0] == {ROOT}


def test_search_snippets_and_raw_diagnostics_do_not_grant_extra_identities():
    calls = [call('search', {'results': [{'url': ROOT, 'snippet': f'[Record]({LEAF})'}],
                             'source_trace': {'candidates': [{'url': LEAF}]}})]
    assert execute_tool(None, 'tofu', TASK, 'custom__fetch', {'url': LEAF}, calls, 7)['error']


@pytest.mark.parametrize('target', ['https://archive.example/原始记录?卷=一',
                                  'https://archive.example/%2f?part=%2f'])
def test_markdown_parser_does_not_rewrite_observed_url_path_or_query(target):
    calls = [chain()[0], call('fetch', {'url': ROOT, 'text': f'[Source]({target})'})]
    discovered, _ = observed_sources(calls)
    assert target in discovered


def test_partial_last_url_line_does_not_invent_a_complete_target():
    calls = [chain()[0], call('fetch', {'url': ROOT, 'text': LEAF, 'truncated': True})]
    assert LEAF not in observed_sources(calls)[0]
    calls[-1]['output']['text'] = f'[Record]({LEAF})'
    assert LEAF in observed_sources(calls)[0]  # Complete syntax still counts.


def test_new_cohort_records_navigation_parser_without_expanding_tool_budgets():
    registration = manifest(Path(__file__).resolve().parents[2], 'model', 'provider', 1, 180)
    assert registration['contract'] == 'tofu.search-agent-diagnostic/v12'
    assert registration['navigation']['parser'] == {
        'distribution': 'markdown-it-py', 'version': metadata.version('markdown-it-py')}
    assert registration['limits']['tool_calls'] == 8
    assert registration['limits']['results'] == 6
    assert registration['limits']['page_chars'] == 20000
    assert len(registration['tools']) == 2


@pytest.mark.parametrize('arm', ['tofu', 'ddgs'])
def test_native_callback_handoff_follows_page_links_and_grades_delivered_evidence(tmp_path, arm):
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from tests.evaluations.test_search_quality import ReplayAPI

    class API(ReplayAPI):
        def call(self, path, body=None):
            if '/events?' in path:
                self.calls.append((path, body))
                return {'events': [
                    {'type': 'tool_wire_projection',
                     'toolNames': [t['function']['name'] for t in tool_schemas()],
                     'executableToolCount': 2, 'schemaFingerprint': 'navigation'},
                    {'type': 'custom_tool_call', 'toolName': 'custom__search',
                     'callId': 'search', 'arguments': {'query': TASK['question']}},
                    *[{'type': 'custom_tool_call', 'toolName': 'custom__fetch',
                       'callId': f'fetch-{i}', 'arguments': {'url': url}}
                      for i, url in enumerate((ROOT, CHAPTER, LEAF))]],
                    'next_cursor': 5, 'caught_up': True, 'done': True}
            result = super().call(path, body)
            if path.endswith('/owned-task'):
                result['content'] = ANSWER
            return result

    calls = []
    def dispatch(called_arm, operation, args, **kwargs):
        calls.append((called_arm, operation, deepcopy(args)))
        if operation == 'search':
            return {'results': [{'url': ROOT}]}
        return {'url': args['url'], 'text': {
            ROOT: f'[Chapter]({CHAPTER})', CHAPTER: f'[Record]({LEAF})', LEAF: QUOTE}[args['url']]}

    api = API()
    runtime = HTTPRuntime(api, {'creator_id': 'test', 'model_id': 'model'}, 'test')
    try:
        report = asyncio.run(run_one(runtime, SimpleNamespace(call=dispatch), TASK,
                                    arm, 0, tmp_path/'run', 30))
        assert report['passed'] and report['provenance_valid']
        assert len(calls) == 4 and all(row[0] == arm for row in calls)
        assert all(row['accepted'] for row in report['calls'])
        assert runtime.in_flight == 0
    finally:
        runtime.close()
