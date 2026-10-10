"""AA routing evidence is exact, fresh, and never overrides parent intent."""
import time

import pytest

pytestmark = pytest.mark.unit


def test_cached_routing_scores_do_not_inherit_base_score(monkeypatch):
    from lib.model_catalog import aa

    monkeypatch.setattr(aa, '_memo', {
        'fetched_at': time.time(),
        'models': aa._parse_dataset({'data': [{
            'name': 'GLM 5.3', 'slug': 'glm-5.3',
            'model_creator': {'name': 'Zhipu'},
            'evaluations': {'artificial_analysis_coding_index': 50},
        }]}),
    })
    scores = aa.cached_routing_scores([
        {'creator_id': 'zhipu', 'model_id': 'glm-5.3'},
        {'creator_id': 'zhipu', 'model_id': 'glm-5.3-flash'},
    ])
    assert scores['zhipu::glm-5.3']['coding'] == 50
    assert 'zhipu::glm-5.3-flash' not in scores
    monkeypatch.setattr(aa, '_memo', {'fetched_at': 1, 'models': []})
    assert aa.cached_routing_scores([{'model_id': 'glm-5.3'}]) == {}


def test_unparented_coder_prefers_measured_coding_quality_over_price(monkeypatch):
    from lib.model_profiles import _selection

    def profile(name, coding, price):
        return {'modelId': name, 'qualityScore': 300, 'roles': ['coder'],
                'autoSelectable': True,
                'aaScores': {'match': 'exact', 'coding': coding, 'intelligence': 50},
                'price': {'known': True, 'blendedUsdPerMTok': price}}

    monkeypatch.setattr(_selection, 'configured_model_profiles', lambda **kw: [
        profile('cheap', 20, 0.1), profile('strong', 60, 5),
    ])
    assert _selection.select_model_for_tier('heavy', role='coder') == 'strong'
    assert _selection.select_model_for_tier(
        'heavy', role='coder', parent_model='user-selected') == 'user-selected'


@pytest.mark.parametrize('raw,expected', [
    (True, None), (False, None), ('52.4', None), (None, None),
    (-1, None), (float('nan'), None), (float('inf'), None),
    (0, 0.0), (52.4, 52.4),
])
def test_invalid_aa_metrics_are_not_coerced_into_ranking_scores(raw, expected):
    from lib.model_catalog import aa

    rows = aa._parse_dataset({'data': [{
        'name': 'fixture', 'slug': 'fixture',
        'evaluations': {
            'artificial_analysis_intelligence_index': raw,
            'artificial_analysis_coding_index': raw,
            'artificial_analysis_agentic_index': raw,
        },
    }]})
    for metric in ('intelligence', 'coding', 'agentic'):
        assert rows[0][metric] == expected
