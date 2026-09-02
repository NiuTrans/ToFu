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


def test_context_composer_wire_envelope_survives_private_field_strip(caplog):
    messages = [
        {'role': 'user', 'content': 'historical user request'},
        {'role': 'user', 'content':
         '<!-- tofu-context:relevant_memories:start -->\n'
         '<system-reminder>managed evidence</system-reminder>\n'
         '<!-- tofu-context:relevant_memories:end -->'},
    ]

    with caplog.at_level(logging.DEBUG):
        merged = _merge_consecutive_same_role(messages)

    assert len(merged) == 1
    assert not any('UNEXPECTED pair' in rec.message for rec in caplog.records)


def test_retained_user_wrapper_survives_private_field_strip(caplog):
    messages = [
        {'role': 'user', 'content': 'managed project context'},
        {'role': 'user', 'content':
         '<retained_user_messages>\nverbatim request\n'
         '</retained_user_messages>'},
    ]

    with caplog.at_level(logging.DEBUG):
        merged = _merge_consecutive_same_role(messages)

    assert len(merged) == 1
    assert not any('UNEXPECTED pair' in rec.message for rec in caplog.records)


def test_compaction_carriers_around_brain_dispatch_are_all_designed(caplog):
    messages = [
        {'role': 'user', 'content': 'the original human objective'},
        {'role': 'user', 'content':
         '<retained_user_messages>\nverbatim request\n'
         '</retained_user_messages>'},
        {'role': 'user', 'content':
         '[Project Brain — autonomous dispatch] pick up the epic'},
    ]

    with caplog.at_level(logging.DEBUG):
        merged = _merge_consecutive_same_role(messages)

    assert len(merged) == 1
    assert not any('UNEXPECTED pair' in rec.message for rec in caplog.records)
    assert any('designed synthetic-context pair' in rec.message
               for rec in caplog.records)


def test_compaction_carriers_around_todo_continuation_are_all_designed(caplog):
    messages = [
        {'role': 'user', 'content': 'the original human objective'},
        {'role': 'user', 'content':
         '<retained_user_messages>\nverbatim request\n'
         '</retained_user_messages>'},
        {'role': 'user', 'content':
         '[SYSTEM: TODO CONTINUATION REQUIRED]\ncomplete the checklist'},
    ]

    with caplog.at_level(logging.DEBUG):
        merged = _merge_consecutive_same_role(messages)

    assert len(merged) == 1
    assert not any('UNEXPECTED pair' in rec.message for rec in caplog.records)
    assert any('designed synthetic-context pair' in rec.message
               for rec in caplog.records)
