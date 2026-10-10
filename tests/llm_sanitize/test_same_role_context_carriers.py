"""Managed context carriers are designed same-role adjacency, not producer faults."""

from __future__ import annotations

import logging

import pytest

from lib.llm_sanitize import _strip_non_api_fields
from lib.llm_sanitize._fields import _SAME_ROLE_SEAM_HINT_FIELD
from lib.llm_sanitize._messages import _merge_consecutive_same_role
from lib.tasks_pkg.wire_messages import apply_wire_sanitize


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


def test_real_duplicate_user_still_warns_after_complete_wire_strip(caplog):
    messages = [
        {'role': 'user', 'content': 'first'},
        {'role': 'user', 'content': 'duplicate from a broken producer'},
    ]

    with caplog.at_level(logging.WARNING):
        wire = apply_wire_sanitize(messages)

    assert wire == [{
        'role': 'user',
        'content': 'first\n\nduplicate from a broken producer',
    }]
    assert any('UNEXPECTED pair' in rec.message for rec in caplog.records)
    assert all(_SAME_ROLE_SEAM_HINT_FIELD not in message for message in wire)


def test_structured_hint_is_opt_in_and_consumed_even_without_adjacency():
    source = [{
        'role': 'user',
        'content': 'managed context',
        '_isMeta': True,
    }]

    strict_api_projection = _strip_non_api_fields(source)
    hinted_projection = _strip_non_api_fields(
        source, carry_same_role_seam_hints=True)

    assert strict_api_projection == [
        {'role': 'user', 'content': 'managed context'},
    ]
    assert hinted_projection[0][_SAME_ROLE_SEAM_HINT_FIELD] is True
    assert _merge_consecutive_same_role(hinted_projection) == (
        strict_api_projection
    )
    assert hinted_projection[0][_SAME_ROLE_SEAM_HINT_FIELD] is True, (
        'merge must clean a returned copy, not mutate its input')


def test_build_body_consumes_relocated_anchor_identity_before_provider(caplog):
    from lib.llm import build_body

    messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'durable objective',
         '_isObjectiveAnchor': True},
        {'role': 'user', 'content': 'current steer'},
    ]

    with caplog.at_level(logging.WARNING):
        body = build_body(
            'qwen-plus', messages, max_tokens=1024, stream=False)

    assert body['messages'] == [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user',
         'content': 'durable objective\n\ncurrent steer'},
    ]
    assert not any('UNEXPECTED pair' in rec.message for rec in caplog.records)
    assert all(
        not any(key.startswith('_tofu') for key in message)
        for message in body['messages']
    )
    assert messages[1]['_isObjectiveAnchor'] is True, (
        'body construction must not consume identity by mutating its caller')


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
