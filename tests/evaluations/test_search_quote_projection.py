"""Observed wording and raw Markdown fidelity are distinct diagnostic facts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from evaluations.search_quality.contracts import PAGE_CHARS, score

pytestmark = pytest.mark.unit
URL = 'https://source.example/reference'


def fixture(source, quote):
    task = {'id': 'synthetic-observed-quote', 'question': 'Find the official answer.',
            'language': 'en', 'region': 'us-en', 'hosts': ['source.example'],
            'facts': {'answer': {'answer': '42', 'quote': ['42']}}}
    calls = [
        {'name': 'custom__search', 'accepted': True, 'is_error': False,
         'output': {'results': [{'url': URL}]}},
        {'name': 'custom__fetch', 'accepted': True, 'is_error': False,
         'output': {'url': URL, 'text': source}},
    ]
    content = json.dumps({'answer': {'value': '42', 'url': URL, 'quote': quote}})
    return task, content, calls


@pytest.mark.parametrize(('source', 'quote'), [
    ('The answer is [42](https://example.net/value) today.', 'The answer is 42 today.'),
    ('The **answer** is [*42*](https://example.net/a_(b) "label") today.',
     'The answer is 42 today.'),
    ('The answer is [42][value] today.\n\n[value]: https://example.net/',
     'The answer is 42 today.'),
    ('The answer is `42` today.', 'The answer is 42 today.'),
    ('Value &amp; rank: **42**.', 'Value & rank: 42.'),
    (r'The answer is \[42\] today.', 'The answer is [42] today.'),
    ('Source <https://example.net/42> is visible.', 'Source https://example.net/42 is visible.'),
    ('官方编号是[42](https://example.net/)，编号保持不变。', '官方编号是42，编号保持不变。'),
    ('公式の番号は**42**です。', '公式の番号は42です。'),
    ('La respuesta es **42**.', 'La respuesta es 42.'),
    ('`**42**` is literal syntax.', '**42** is literal syntax.'),
])
def test_inline_presentation_does_not_hide_observed_verbatim_wording(source, quote):
    result = score(*fixture(source, quote))
    assert result['passed']
    fact = result['facts']['answer']
    assert fact['quote_observed']
    assert not fact['quote_raw_observed']
    assert fact['quote_rendered_observed']


@pytest.mark.parametrize(('source', 'quote'), [
    ('The answer is **not** 42 today.', 'The answer is 42 today.'),
    ('The answer is **42** today.', 'Today the answer is 42.'),
    ('The answer is **42**, today.', 'The answer is 42 today.'),
    ('The answer is [unknown](https://example.net/42) today.', 'The answer is 42 today.'),
    ('The answer is [unknown](https://example.net "42") today.', 'The answer is 42 today.'),
    ('The answer is ![42](https://example.net/image) today.', 'The answer is 42 today.'),
    ('The answer is **42**\n\ntoday.', 'The answer is 42 today.'),
    ('- The answer is **42**\n- today.', 'The answer is 42 today.'),
    ('The answer is **42** [...] today.', 'The answer is 42 today.'),
    ('The answer is **42** ![](https://example.net/image) today.', 'The answer is 42 today.'),
    ('```markdown\nThe answer is **42** today.\n```', 'The answer is 42 today.'),
    ('The answer is `**42**` today.', 'The answer is 42 today.'),
    ('The answer is <b>42</b> today.', 'The answer is 42 today.'),
    ('| Answer | **42** | today |', 'Answer 42 today'),
])
def test_rendered_projection_does_not_invent_a_contiguous_quote(source, quote):
    result = score(*fixture(source, quote))
    assert not result['passed']
    assert not result['facts']['answer']['quote_observed']


@pytest.mark.parametrize('fault', ['no_search', 'no_fetch', 'error', 'rejected',
                                  'future_discovery', 'wrong_url', 'over_budget'])
def test_rendered_match_cannot_bypass_chronological_source_authority(fault):
    task, content, calls = fixture('The answer is **42** today.', 'The answer is 42 today.')
    if fault == 'no_search':
        calls.pop(0)
    elif fault == 'no_fetch':
        calls.pop()
    elif fault == 'error':
        calls[-1]['is_error'] = True
    elif fault == 'rejected':
        calls[-1]['accepted'] = False
    elif fault == 'future_discovery':
        calls.reverse()
    elif fault == 'wrong_url':
        calls[-1]['output']['url'] = 'https://other.example/reference'
    elif fault == 'over_budget':
        calls[-1]['output']['text'] += 'x' * PAGE_CHARS
    assert not score(task, content, calls)['facts']['answer']['quote_observed']


def test_separate_fetches_cannot_be_stitched_into_one_rendered_quote():
    task, content, calls = fixture('The answer is **42**', 'The answer is 42 today.')
    calls.append({'name': 'custom__fetch', 'accepted': True, 'is_error': False,
                  'output': {'url': URL, 'text': 'today.'}})
    assert not score(task, content, calls)['facts']['answer']['quote_observed']


def test_literal_markdown_quotes_keep_raw_match_credit():
    source = 'The answer is [42](https://example.net/value) today.'
    result = score(*fixture(source, source))
    assert result['passed']
    assert result['facts']['answer']['quote_raw_observed']


@pytest.mark.parametrize('arm', ['tofu', 'ddgs'])
@pytest.mark.parametrize('accepted', [True, False])
def test_native_callback_keeps_visible_matching_inside_source_authority(tmp_path, arm, accepted):
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.search_quality.runner import run_one
    from tests.evaluations.test_search_quality import ReplayAPI

    task, content, calls = fixture('The answer is **42** today.', 'The answer is 42 today.')
    class API(ReplayAPI):
        def call(self, path, body=None):
            result = super().call(path, body)
            if '/events?' in path:
                for event in result['events']:
                    if event.get('toolName') == 'custom__fetch':
                        event['arguments']['url'] = URL
            if path.endswith('/owned-task'):
                result['content'] = content
            return result
    def dispatch(called_arm, operation, args, **kwargs):
        assert called_arm == arm
        return calls[0 if operation == 'search' else 1]['output']
    runtime = HTTPRuntime(API(refuse_callback=not accepted),
                          {'creator_id': 'test', 'model_id': 'model'}, 'test')
    try:
        report = asyncio.run(run_one(runtime, SimpleNamespace(call=dispatch), task,
                                    arm, 0, tmp_path/'run', 30))
        assert report['passed'] is accepted
        assert report['score']['facts']['answer']['quote_rendered_observed'] is accepted
        assert not report['score']['facts']['answer']['quote_raw_observed']
        assert runtime.in_flight == 0
    finally:
        runtime.close()
