"""Managed context carriers are designed same-role adjacency, not producer faults."""

from __future__ import annotations

import logging

import pytest

from lib.llm_sanitize._messages import _merge_consecutive_same_role


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('marker', [
    {'_contextComposer': True},
    {'_isMeta': True},
    {'_isVuDirective': True},
])
def test_structured_synthetic_carrier_merges_without_warning(marker, caplog):
    messages = [
        {'role': 'user', 'content': 'historical user request'},
        {'role': 'user', 'content': 'managed context', **marker},
    ]

    with caplog.at_level(logging.DEBUG):
        merged = _merge_consecutive_same_role(messages)

    assert len(merged) == 1
    assert merged[0]['content'] == 'historical user request\n\nmanaged context'
    assert not any('UNEXPECTED pair' in rec.message for rec in caplog.records)
    assert any('designed synthetic-context pair' in rec.message
               for rec in caplog.records)


def test_real_duplicate_user_still_warns(caplog):
    messages = [
        {'role': 'user', 'content': 'first'},
        {'role': 'user', 'content': 'duplicate from a broken producer'},
    ]

    with caplog.at_level(logging.WARNING):
        _merge_consecutive_same_role(messages)

    assert any('UNEXPECTED pair' in rec.message for rec in caplog.records)
