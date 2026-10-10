import hashlib

import json
from types import SimpleNamespace

import pytest
from lib.paper.survey import _survey_working_set
from lib.research.survey_hierarchy import map_reduce, BoundedCalls
from lib.research.entailment import verify_quote, assess_gaps
from lib.research.telemetry import ResearchUsageMeter


pytestmark = pytest.mark.unit

def papers(n):
    return [dict(arxiv_id=f'2501.{i:05}', title='Test', content='However a competing solution works. ' * 30,
                 source='parsed_text', paper_hash=str(i), source_version='v1') for i in range(n)]


def fake(phase, payload):
    if phase == 'synthesis':
        return {'survey_md': 'Survey', 'gap_map': {'open_gaps': []}}
    return {'summary': 'summary', 'support': 'support', 'counterevidence': 'competing solution'}


def test_all_65_and_partial_batches():
    seen = []
    def call(phase, payload):
        import json
        assert len(json.dumps(payload)) < 40000
        if phase == 'map':
            seen.extend(c['arxiv_id'] for c in payload['cards'])
        return fake(phase, payload)
    result = map_reduce(papers(65), 'solution', call, _survey_working_set)
    assert len(result[2]) == 65
    assert len(set(seen)) == 65
    assert not result[4]
    count = 0
    def fail(phase, payload):
        nonlocal count
        if phase == 'map':
            count += 1
            if count == 2:
                raise RuntimeError('offline failure')
        return fake(phase, payload)
    partial = map_reduce(papers(65), 'solution', fail, _survey_working_set)
    assert 0 < len(partial[2]) < 65
    assert partial[4]


def test_identity_and_late_counterevidence():
    source = papers(30)
    result = map_reduce(source, 'solution', fake, _survey_working_set)
    cards = result[3]
    sources = {p['arxiv_id']: p for p in source}
    assert verify_quote(cards[0], sources)
    assert not verify_quote(dict(cards[0], quote='forged'), sources)
    assert not verify_quote({k: v for k, v in cards[0].items() if k != 'quote'}, sources)
    late = cards[-1]['card_id']
    def assess(phase, payload):
        return {'assessments': [dict(card_id=c['card_id'],
            status='contradicted' if c['card_id'] == late else 'insufficient')
            for c in payload['cards']]}
    gaps = [{'gap': 'No solution'}]
    assess_gaps(gaps, cards, sources, assess)
    assert gaps[0]['status'] == 'closed'
    assert gaps[0]['assessment'] == 'model_assessed'


def test_checkpoint_resume(tmp_path):
    dispatches = []
    def dispatch(*args, **kwargs):
        dispatches.append(args)
        return ({'role': 'assistant', 'content': '{"summary":"cached"}'}, 'stop', {})
    import json
    def caller():
        return BoundedCalls(dispatch, json.loads, ResearchUsageMeter('survey'),
                            direction='q', user_id=1, workdir=tmp_path, sources=['v1'])
    assert caller()('map', {'x': 1}) == {'summary': 'cached'}
    resumed = caller()
    assert resumed('map', {'x': 1}) == {'summary': 'cached'}
    assert len(dispatches) == 1
    assert resumed.cache_hits == 1
    resumed('map', {'x': 2})
    assert len(dispatches) == 2


def test_hierarchy_enters_real_dispatcher_once(monkeypatch):
    """A zero retries value must not silently suppress the initial attempt."""
    from lib.llm_dispatch import api
    from lib.llm_dispatch import conv_affinity

    picks = []

    class ProviderPoolReached(RuntimeError):
        pass

    def pick(**kwargs):
        picks.append(kwargs)
        raise ProviderPoolReached('test provider pool reached')

    monkeypatch.setattr(api, 'get_dispatcher', lambda: SimpleNamespace(
        summarize_slots=lambda *_: 'offline test', pick_and_reserve=pick))
    monkeypatch.setattr(conv_affinity, 'sticky_routing_enabled', lambda: False)
    meter = ResearchUsageMeter('survey')
    call = BoundedCalls(api.dispatch_stream, json.loads, meter,
                        direction='q', user_id=7, model='test-model')
    with pytest.raises(ProviderPoolReached):
        call('map', {'cards': []})
    assert len(picks) == 1
    assert picks[0]['strict_model'] is True
    assert picks[0]['prefer_model'] == 'test-model'
    assert meter.agent_dispatches == 1


def test_hierarchy_dispatch_remains_owner_scoped_and_bounded():
    calls = []
    def dispatch(*args, **kwargs):
        calls.append(kwargs)
        return ({'role': 'assistant', 'content': '{"summary":"ok"}'}, 'stop', {})
    abort = lambda: False
    meter = ResearchUsageMeter('survey')
    call = BoundedCalls(dispatch, json.loads, meter,
                        direction='q', user_id=7, model='test-model', abort=abort)
    assert call('map', {'cards': []}) == {'summary': 'ok'}
    assert calls[0]['max_retries'] == 1
    assert 1 <= calls[0]['max_429_attempts'] <= 64
    assert calls[0]['owner_user_id'] == 7
    assert calls[0]['abort_check'] is abort
    assert calls[0]['max_tokens'] == 4000


def test_all_failed_maps_preserve_underlying_error_without_publishing():
    phases = []
    def fail(phase, payload):
        phases.append(phase)
        raise ValueError('upstream test failure')
    with pytest.raises(RuntimeError, match='no successful survey map batches') as caught:
        map_reduce(papers(65), 'solution', fail, _survey_working_set)
    assert 'ValueError: upstream test failure' in str(caught.value)
    assert len(str(caught.value)) < 1500
    assert all(phase == 'map' for phase in phases)
    failures = caught.value.failures
    assert len(failures) == len(phases)
    assert len({aid for row in failures for aid in row['paper_ids']}) == 65


def test_build_survey_returns_total_map_failure_receipts(monkeypatch):
    from lib.paper import survey
    monkeypatch.setattr(survey, '_load_paper_inputs', lambda *a, **k: papers(2))
    def unavailable(*args, **kwargs):
        raise ValueError('provider unavailable in offline test')
    monkeypatch.setattr(survey, 'dispatch_stream', unavailable)
    result = survey.build_survey('solution', [], user_id=7)
    assert result['ok'] is False
    assert result['survey_md'] == ''
    assert result['open_gaps'] == {}
    assert 'provider unavailable in offline test' in result['error']
    assert result['batch_failures'][0]['paper_ids'] == ['2501.00000', '2501.00001']
