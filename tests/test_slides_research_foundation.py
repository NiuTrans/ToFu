"""Grounded research is a real first-class slide stage."""

from __future__ import annotations

import pytest

import lib.slides.recipe as recipe
from lib.production.research import (
    evidence_checkpoint_version,
    research_topic,
    summarise_current_signals,
)

pytestmark = pytest.mark.unit


def test_research_keeps_only_url_grounded_unique_cards(monkeypatch):
    calls = []

    def _search(query, **kwargs):
        calls.append((query, kwargs.get('freshness')))
        return [
            {'title': 'Primary', 'url': 'https://example.com/2026/08/09/a',
             'snippet': 'Grounded fact.', 'published_at': '2026-08-09'},
            {'title': 'Duplicate', 'url': 'https://example.com/2026/08/09/a',
             'snippet': 'Duplicate.'},
            {'title': 'No URL', 'snippet': 'Must be dropped.'},
        ]

    monkeypatch.setattr(
        'tofu_search.perform_web_search',
        _search)
    out = research_topic('topic')
    assert out['degraded'] is False
    assert out['cards'] == [{
        'id': 'S1', 'title': 'Primary', 'point': 'Grounded fact.',
        'url': 'https://example.com/2026/08/09/a', 'host': 'example.com',
        'published_at': '2026-08-09', 'query_lane': 'current',
        'query_lanes': ['current', 'official', 'background'],
        'freshness': 'month', 'source_hints': []}]
    assert len(calls) == 3
    assert any(freshness == 'month' for _, freshness in calls)
    assert any(query == 'topic' and freshness == ''
               for query, freshness in calls)
    assert out['as_of']
    assert [q['lane'] for q in out['queries']] == [
        'current', 'official', 'background']
    assert next(q for q in out['queries'] if q['lane'] == 'official')['deepen']


def test_outline_receives_cards_and_attaches_referenced_sources(monkeypatch):
    seen = {}
    reply = (
        '{"title":"T","scenario":"tech-engineering","pages":['
        '{"pageType":"cover","key_message":"Open","content_notes":"[S1] fact"},'
        '{"pageType":"content","key_message":"Explain","content_notes":"[S1] fact"},'
        '{"pageType":"final","key_message":"Act","content_notes":"end"}]}'
    )

    def _llm(messages, **kwargs):
        seen['prompt'] = messages[0]['content']
        return reply, {}

    monkeypatch.setattr(recipe, '_llm_chat', _llm)
    card = {'id': 'S1', 'title': 'Source', 'point': 'fact',
            'url': 'https://example.com/a', 'host': 'example.com',
            'published_at': '2026-08-09', 'query_lane': 'current',
            'query_lanes': ['current'], 'freshness': 'month'}
    ctx = {'topic': 'T', 'lang': 'zh', 'max_pages': 6, 'style': '',
           'artifacts': {'research': {'cards': [card],
                                      'as_of': '2026-08-10T12:00:00+08:00'}},
           '_outline_gate_feedback': ['must fix current price']}
    out = recipe._run_outline(ctx)
    assert '[S1] fact' in seen['prompt']
    assert 'published: 2026-08-09' in seen['prompt']
    assert '研究截止时间: 2026-08-10T12:00:00+08:00' in seen['prompt']
    assert '严格区分预售价、最终售价、传闻和估算' in seen['prompt']
    assert 'single-host price candidates=none' in seen['prompt']
    assert 'Previous outline attempt was rejected' in seen['prompt']
    assert 'must fix current price' in seen['prompt']
    assert '_outline_gate_feedback' not in ctx
    assert out['pages'][1]['sources'] == [card]
    assert out['pages'][-1]['sources'] == []


def test_current_price_signal_rejects_outline_that_ignores_it():
    card = {
        'id': 'S1', 'title': '官方预售',
        'point': 'N70 Max 预售价 25.99 万元，现已开启预售。',
        'url': 'https://example.com/latest', 'host': 'example.com',
        'published_at': '2026-07-30', 'query_lane': 'current',
        'query_lanes': ['current'], 'freshness': 'month',
    }
    signals = summarise_current_signals([card])
    assert signals['price_values'] == ['25.99万']
    stale = {
        'pages': [
            {'purpose': '开场', 'key_message': '产品亮相', 'content_notes': ''},
            {'purpose': '价格', 'key_message': '预售价尚未公布',
             'content_notes': '等待更多消息'},
            {'purpose': '收束', 'key_message': '继续关注', 'content_notes': ''},
        ],
    }
    ctx = {'artifacts': {'research': {
        'cards': [card], 'current_signals': signals}}}
    errors = recipe._gate_outline(ctx, stale)
    assert any('ignores every current-state source' in e for e in errors)
    assert any('does not acknowledge a presale/price announcement' in e
               for e in errors)
    assert any('announced presale price' in e for e in errors)
    assert ctx['_outline_gate_feedback'] == errors


def test_current_price_signal_passes_when_cited_and_precisely_stated():
    card = {
        'id': 'S1', 'title': '官方预售',
        'point': 'N70 Max 预售价 25.99 万元。',
        'url': 'https://example.com/latest', 'host': 'example.com',
        'published_at': '2026-07-30', 'query_lane': 'official',
        'query_lanes': ['official'], 'freshness': 'none',
    }
    ctx = {'artifacts': {'research': {'cards': [card]}}}
    fresh = {
        'pages': [
            {'purpose': '开场', 'key_message': '产品亮相', 'content_notes': ''},
            {'purpose': '价格', 'key_message': '预售价格已经明确',
             'content_notes': '[S1] N70 Max 预售价 25.99 万元'},
            {'purpose': '收束', 'key_message': '选择适合的版本',
             'content_notes': ''},
        ],
    }
    assert recipe._gate_outline(ctx, fresh) == []


def test_research_queries_subject_not_creative_or_model_instructions(monkeypatch):
    calls = []

    def _search(query, **kwargs):
        calls.append(query)
        return [{
            'title': '旧报道',
            'url': 'https://example.com/2026-07-12/story',
            'snippet': '预计将于2026年7月30日正式上市。',
        }]

    monkeypatch.setattr(
        'tofu_search.perform_web_search', _search)
    out = research_topic('小米澎程 SkyNomad 汽车宣传片和视频，都用 kimi k3')
    assert out['subject'] == '小米澎程 SkyNomad 汽车'
    assert len(calls) == 3
    assert all('宣传片和视频' not in query and 'kimi' not in query.lower()
               for query in calls)
    # The event date inside the prose is not the article publication date.
    assert out['cards'][0]['published_at'] == '2026-07-12'
    assert out['current_signals']['launched_source_ids'] == []


def test_research_reserves_half_the_card_budget_for_official_candidates(
        monkeypatch):
    def _results(prefix):
        return [
            {'title': f'{prefix}-{i}', 'url': f'https://{prefix}.example/{i}',
             'snippet': f'{prefix} fact {i}'}
            for i in range(1, 7)
        ]

    def _search(query, **kwargs):
        if 'official website' in query:
            return _results('official')
        if kwargs.get('freshness') == 'month':
            return _results('current')
        return _results('background')

    monkeypatch.setattr(
        'tofu_search.perform_web_search', _search)
    out = research_topic('product story', max_cards=12)
    titles = [card['title'] for card in out['cards']]
    assert titles[:4] == [f'current-{i}' for i in range(1, 5)]
    assert titles[4:10] == [f'official-{i}' for i in range(1, 7)]
    assert titles[10:] == ['background-1', 'background-2']


def test_price_signal_separates_cross_host_consensus_from_single_source():
    cards = [
        {'id': 'S1', 'title': '预售价 25.99 万元 / 29.99 万元',
         'point': '现已开启预售', 'host': 'first.example',
         'query_lanes': ['current']},
        {'id': 'S2', 'title': '预售价25.99万、29.99万',
         'point': '价格已经公布', 'host': 'second.example',
         'query_lanes': ['official']},
        {'id': 'S3', 'title': '预售价29.9万元',
         'point': '单一摘要', 'host': 'third.example',
         'query_lanes': ['current']},
    ]
    signals = summarise_current_signals(cards)
    assert signals['corroborated_price_values'] == ['25.99万', '29.99万']
    assert signals['single_source_price_values'] == ['29.9万']
    assert signals['price_evidence'][0]['hosts'] == [
        'first.example', 'second.example']


def test_speculative_price_is_not_promoted_to_current_fact():
    cards = [{
        'id': 'S1', 'title': '网友猜测价格可能为40万元',
        'point': '最终价格仍以官方为准', 'host': 'rumor.example',
        'query_lanes': ['current'],
    }]
    signals = summarise_current_signals(cards)
    assert signals['price_values'] == []
    assert signals['price_source_ids'] == []


def test_deposit_is_not_counted_as_product_price():
    cards = [{
        'id': 'S1', 'title': '预售价29.99万元，支付1000元意向金开启预订',
        'point': '现已开启预售', 'host': 'sales.example',
        'query_lanes': ['current'],
    }]
    signals = summarise_current_signals(cards)
    assert signals['price_values'] == ['29.99万']


def test_official_lane_keeps_named_product_url_even_when_ranked_last(monkeypatch):
    def _search(query, **kwargs):
        if 'official website' not in query:
            return []
        rows = [
            {'title': f'Article {i}', 'url': f'https://news.example/{i}',
             'snippet': 'secondary article'}
            for i in range(1, 12)
        ]
        rows.append({
            'title': 'Named product page',
            'url': 'https://maker.example/skynomad/n90',
            'snippet': 'product details',
        })
        return rows

    monkeypatch.setattr(
        'tofu_search.perform_web_search', _search)
    out = research_topic('SkyNomad launch deck', max_cards=6)
    assert out['cards'][0]['title'] == 'Named product page'
    assert out['cards'][0]['source_hints'] == [
        'subject-token-in-url:skynomad']


def test_research_is_the_first_recipe_stage():
    stages = recipe.slides_recipe_stages()
    names = [stage.name for stage in stages]
    assert names[:2] == ['research', 'outline']
    assert stages[0].resume_ttl_s == 6 * 60 * 60
    assert stages[0].checkpoint_version == evidence_checkpoint_version(
        freshness='month')
