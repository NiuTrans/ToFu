"""Shared content contracts for deck and motion production.

Regression anchor: the SkyNomad deck/video review on 2026-08-10 exposed that
the two media paths had different freshness, citation, asset and QA schemas.
The shared substrate must own those cross-media contracts while renderers
remain capability-specific.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_slides_research_path_is_a_compatibility_shim():
    """There must be one implementation, not two synchronized copies."""
    from lib.production import research as shared
    from lib.slides import _research as legacy

    assert legacy.research_topic is shared.research_topic
    assert legacy.summarise_current_signals is shared.summarise_current_signals


def test_shared_research_supports_media_specific_freshness_profiles():
    from lib.production.research import research_topic

    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs.get('freshness'), kwargs.get('deepen')))
        return [{
            'title': 'Grounded update',
            'url': 'https://example.com/2026/08/10/update',
            'snippet': 'Officially announced current status.',
        }]

    out = research_topic(
        'Product film', current_freshness='week',
        fallback_unfiltered_current=True, search_fn=search)

    assert out['cards'][0]['id'] == 'S1'
    assert any(freshness == 'week' for _, freshness, _ in calls)
    assert any(deepen is True for _, _, deepen in calls)
    # The unfiltered retry found the same URL, so the retained card keeps its
    # stronger week provenance even though the attempt is logged separately.
    assert out['freshness_used'] == 'week'


def test_starved_fallback_never_discards_stronger_fresh_cards():
    from lib.production.research import research_topic

    def search(query, **kwargs):
        if kwargs.get('freshness') == 'week':
            return [
                {'title': f'Fresh {index}',
                 'url': f'https://fresh.example/{index}',
                 'snippet': f'current fact {index}'}
                for index in (1, 2)
            ]
        return []

    out = research_topic(
        'News subject', current_freshness='week',
        fallback_unfiltered_current=True, search_fn=search)

    assert len(out['cards']) == 2
    assert all(card['freshness'] == 'week' for card in out['cards'])
    assert out['freshness_used'] == 'week'
    assert out['degraded'] is False


def test_shared_current_fact_gate_requires_current_citation_and_price_status():
    from lib.production.research import current_fact_errors

    card = {
        'id': 'S1', 'title': 'Official presale',
        'point': 'N70 Max 预售价 25.99 万元，现已开启预售。',
        'url': 'https://example.com/latest', 'host': 'example.com',
        'query_lane': 'current', 'query_lanes': ['current'],
    }
    research = {'cards': [card]}
    errors = current_fact_errors(
        research, '价格尚未公布，敬请期待。', cited_ids=[])
    assert any('ignores every current-state source' in error for error in errors)
    assert any('announced presale price' in error for error in errors)

    assert current_fact_errors(
        research, '[S1] N70 Max 预售价 25.99 万元。', cited_ids=['S1']) == []


def test_shared_contracts_normalise_narrative_assets_sources_and_findings():
    from lib.production.contracts import (
        normalise_asset_briefs,
        normalise_findings,
        normalise_narrative_core,
        normalise_source_ids,
    )

    unit = {'narrative_role': 'invented', 'narrative_why': '  prove   it  '}
    normalise_narrative_core(
        unit, allowed_roles=('hook', 'evidence'), fallback_role='evidence',
        fallback_why='Ground the claim.')
    assert unit == {'narrative_role': 'evidence',
                    'narrative_why': 'prove it'}
    assert normalise_source_ids(['S2', 'bad', 'S2', 'S1'],
                                valid_ids=('S1', 'S2')) == ['S2', 'S1']
    assert normalise_asset_briefs(
        [{'role': 'hero', 'prompt': '  real   product  ',
          'semantic_target': '  visible   floor rail '}],
        allowed_roles=('subject', 'background'), fallback_role='background') == [
            {'role': 'background', 'prompt': 'real product',
             'semantic_target': 'visible floor rail'}]
    assert normalise_findings(
        [{'check': 'overflow', 'issue': ' clipped ', 'severity': 'fatal'}],
        valid_checks=('overflow',)) == [{
            'check': 'overflow', 'element': '', 'issue': 'clipped',
            'severity': 'minor', 'fix': '',
        }]
