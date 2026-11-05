"""A failed metadata write must not re-price already recorded report work."""

from copy import deepcopy

import pytest


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['missing', 'unavailable'])
def test_metadata_fallback_keeps_component_costs_and_one_price_lookup(monkeypatch, failure):
    from lib import cost
    from lib.paper import artifact_repository
    from lib.paper.report_engine import _hooks

    metadata = {
        'model': 'body-model', 'providerId': 'provider-a',
        'promptTokens': 100, 'completionTokens': 20,
        'costCny': 10, 'costUsd': 2,
        'secondPasses': {'prior': {
            'usage': {'prompt_tokens': 7}, 'costCny': 3, 'costUsd': 0.6}},
    }
    task = {'_userId': 17, 'lang': 'en', 'report_meta': deepcopy(metadata)}
    usage = {'prompt_tokens': 2, 'completion_tokens': 3}
    priced, writes, events = [], [], []

    def price(value, **kwargs):
        priced.append((deepcopy(value), kwargs))
        return {'costCny': 0.5, 'costUsd': 0.1}

    class Repository:
        def __init__(self, owner):
            assert owner == 17

        def merge_report_second_pass(self, phash, lang, name, entry, *, command_id):
            writes.append((phash, lang, name, deepcopy(entry)))
            assert command_id
            if failure == 'unavailable':
                raise OSError('authority unavailable')
            return None

    monkeypatch.setattr(cost, 'compute_cost', price)
    monkeypatch.setattr(artifact_repository, 'PaperArtifactRepository', Repository)
    monkeypatch.setattr(_hooks, '_append_report_event', lambda _, event: events.append(event))
    _hooks._merge_second_pass(task, 'paper-a', 'insight', {'usage': usage}, model='pass-model')

    result = task['report_meta']
    assert result['totalCostCny'] == pytest.approx(13.5)
    assert result['totalCostUsd'] == pytest.approx(2.7)
    assert result['totalUsage'] == {
        'prompt_tokens': 109, 'completion_tokens': 23,
        'cache_read_tokens': 0, 'cache_write_tokens': 0, 'reasoning_tokens': 0}
    assert result['secondPasses']['prior'] == metadata['secondPasses']['prior']
    assert priced == [(usage, {'model_id': 'pass-model', 'provider_id': 'provider-a'})]
    assert writes == [('paper-a', 'en', 'insight', {
        'usage': usage, 'costCny': 0.5, 'costUsd': 0.1})]
    assert events == [{'type': 'report_meta', 'paperHash': 'paper-a', 'meta': result}]


def test_confirmed_metadata_is_not_rebuilt_from_a_stale_task(monkeypatch):
    from lib.paper import artifact_repository
    from lib.paper.report_engine import _hooks

    confirmed = {'secondPasses': {'deepen': {'calls': 3}}, 'totalCostCny': 9}
    task = {'_userId': 17, 'lang': 'en', 'report_meta': {'secondPasses': {}}}
    events = []

    class Repository:
        def __init__(self, owner):
            assert owner == 17

        def merge_report_second_pass(self, *args, **kwargs):
            return confirmed

    monkeypatch.setattr(artifact_repository, 'PaperArtifactRepository', Repository)
    monkeypatch.setattr(_hooks, '_append_report_event', lambda _, event: events.append(event))
    _hooks._merge_second_pass(task, 'paper-a', 'insight', {'fired': False})
    assert task['report_meta'] is confirmed
    assert events[0]['meta'] is confirmed
