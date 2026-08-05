"""Vertical SSE payload shape pins — ``_vertical_to_sse_payload``.

The auto-detect path (``resolve_vertical`` with ``vertical='auto'``) returns
TYPE-level records (``items`` but no ``sources``). The legacy wrap branch must
pass a handler's own rich rows through — travel flight/hotel records carry
bookable items — instead of collapsing the record to one synthesized headline.
"""

import pytest

from lib.tasks_pkg.handlers.search._display import _vertical_to_sse_payload


@pytest.mark.unit
def test_type_level_record_items_pass_through():
    record = {
        'domain': 'travel', 'type': 'flight',
        'identifier': '北京→上海@2026-08-06',
        'source': '飞猪机票 (FlyAI)',
        'content': '## 航班 北京 → 上海  2026-08-06\n…',
        'items': [
            {'title': 'MU5231 大兴→浦东', 'snippet': '¥410 · 08-06 23:20',
             'url': 'https://router.feizhu.com/a', 'type': 'flight',
             'bookable': True},
            {'title': 'CA8341 大兴→浦东', 'snippet': '¥450 · 08-06 22:00',
             'url': 'https://router.feizhu.com/b', 'type': 'flight',
             'bookable': True},
        ],
    }
    out = _vertical_to_sse_payload(record)
    assert out is not None
    assert out['domain'] == 'travel'
    assert out['sources'] == [{'type': 'flight', 'source': '飞猪机票 (FlyAI)',
                               'identifier': '北京→上海@2026-08-06'}]
    assert [i['url'] for i in out['items']] == [
        'https://router.feizhu.com/a', 'https://router.feizhu.com/b']
    assert out['items'][0]['bookable'] is True
    assert out['items'][0]['type'] == 'flight'
    assert out['items'][0]['source'] == '飞猪机票 (FlyAI)'
    # Items are copied, not aliased — downstream mutation must not leak back.
    assert out['items'][0] is not record['items'][0]


@pytest.mark.unit
def test_item_missing_type_and_source_get_record_defaults():
    record = {'domain': 'travel', 'type': 'hotel',
              'source': '飞猪酒店 (FlyAI)', 'content': '## 酒店 …',
              'items': [{'title': '海友上海外滩延安东路酒店', 'snippet': '¥2xx',
                         'url': 'https://router.feizhu.com/c'}]}
    out = _vertical_to_sse_payload(record)
    assert out['items'][0]['type'] == 'hotel'
    assert out['items'][0]['source'] == '飞猪酒店 (FlyAI)'
    assert out['items'][0]['url'] == 'https://router.feizhu.com/c'


@pytest.mark.unit
def test_record_without_items_still_falls_back_to_headline():
    """The synthesis fallback survives for content-only records."""
    record = {'domain': 'finance', 'type': 'stock', 'source': 'Yahoo Finance',
              'identifier': 'AAPL',
              'content': '## Apple Inc (AAPL)\n\n**价格**: 232.78 USD'}
    out = _vertical_to_sse_payload(record)
    assert out is not None
    assert len(out['items']) == 1
    assert out['items'][0]['title'] == 'Apple Inc (AAPL)'
    assert out['items'][0]['type'] == 'stock'
    assert out['items'][0]['source'] == 'Yahoo Finance'


@pytest.mark.unit
def test_domain_level_record_passes_through_untouched():
    record = {'domain': 'travel',
              'sources': [{'type': 'flight', 'source': '飞猪机票 (FlyAI)'}],
              'items': [{'title': 'x', 'url': 'u'}],
              'content': '…'}
    out = _vertical_to_sse_payload(record)
    assert out['items'] == [{'title': 'x', 'url': 'u'}]
    assert out['sources'][0]['source'] == '飞猪机票 (FlyAI)'


@pytest.mark.unit
def test_empty_and_non_dict_records_return_none():
    assert _vertical_to_sse_payload(None) is None
    assert _vertical_to_sse_payload('nope') is None
