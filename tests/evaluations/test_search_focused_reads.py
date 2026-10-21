"""The paired agent diagnostic exposes focused reads without widening authority."""
from types import SimpleNamespace

import jsonschema
import pytest

from evaluations.search_quality.contracts import PAGE_CHARS, tasks, tool_schemas
from evaluations.search_quality.runner import execute_tool

pytestmark = pytest.mark.unit


def discovered():
    return [{'name': 'custom__search', 'accepted': True, 'is_error': False,
             'output': {'results': [{'url': 'https://example.com/manual'}]}}]


@pytest.mark.parametrize('query', [None, '', '缓存延迟 C++', 'q' * 500],
                         ids=['omitted', 'empty', 'multilingual', 'maximum'])
@pytest.mark.parametrize('arm', ['tofu', 'ddgs'])
def test_focused_read_schema_and_callback_preserve_optional_query(arm, query):
    args = {'url': 'https://example.com/manual'}
    if query is not None:
        args['query'] = query
    schema = next(t['function']['parameters'] for t in tool_schemas()
                  if t['function']['name'] == 'custom__fetch')
    jsonschema.validate(args, schema)
    dispatched = []

    def call(*positional, **options):
        dispatched.append((positional, options))
        return {'text': 'Original evidence.'}

    assert execute_tool(SimpleNamespace(call=call), arm, tasks()[0],
                        'custom__fetch', args, discovered(), 7)['text'] == 'Original evidence.'
    assert dispatched == [((arm, 'fetch', args), {'region': tasks()[0]['region'], 'seconds': 7})]


@pytest.mark.parametrize('extra', [{'query': None}, {'query': 3}, {'query': {}},
                                 {'query': 'q' * 501}, {'secret': 'not admitted'}])
def test_invalid_focus_arguments_never_dispatch(extra):
    args = {'url': 'https://example.com/manual', **extra}
    schema = next(t['function']['parameters'] for t in tool_schemas()
                  if t['function']['name'] == 'custom__fetch')
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, schema)
    assert execute_tool(None, 'tofu', tasks()[0], 'custom__fetch',
                        args, discovered(), 10) == {'error': 'invalid_arguments'}


def test_focused_query_cannot_authorize_an_undiscovered_url():
    assert execute_tool(None, 'tofu', tasks()[0], 'custom__fetch',
                        {'url': 'https://example.com/other', 'query': 'manual'},
                        discovered(), 10) == {'error': 'url_not_returned_by_search'}


@pytest.mark.parametrize('arm', ['tofu', 'ddgs'])
@pytest.mark.parametrize('focused', [False, True])
def test_worker_uses_common_fetch_selection_and_preserves_partial_evidence(monkeypatch, arm, focused):
    from evaluations.search_quality import worker
    import tofu_search
    from tofu_search.fetch import core, utils

    fact = '缓存延迟为 17 毫秒；该值不能推断吞吐量。'
    body = 'Unrelated source material.\n\n' * 4000 + fact
    requests = []
    closed = []

    def read(url, *_args, **_kwargs):
        requests.append(url)
        return SimpleNamespace(headers={'Content-Type': 'text/plain'}, encoding='utf-8'), body.encode()

    monkeypatch.setattr(worker.os, 'environ', dict(worker.os.environ))
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_get_reader', lambda _url: None)
    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: None)
    monkeypatch.setattr(core, '_do_request', read)
    monkeypatch.setattr(tofu_search, 'get_playwright_pool', lambda: SimpleNamespace(
        shutdown=lambda: closed.append(True)))
    request = {'arm': arm, 'operation': 'fetch', 'url': 'https://example.com/manual',
               'proxy': '', 'seed': 0}
    if focused:
        request['query'] = '缓存延迟'
    result = worker.operation(request)
    assert (fact in result['text']) is focused
    assert result['truncated'] is True
    assert len(result['text']) <= PAGE_CHARS
    assert requests == ['https://example.com/manual']
    assert closed == [True]
