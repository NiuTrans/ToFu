"""Edge cases of the shared Turn page planner that a stored Turn rarely reaches.

The authority-level contract lives in ``test_turn_projection_pages.py``; this
file pins the planner's indivisible-head rule and the members it never pages.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.storage.turn_projection_pages import turn_projection_page

pytestmark = [pytest.mark.unit]

_FRAME_PAYLOAD_BYTES = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts/storage_v2.json').read_text()
)['frame']['maximumPayloadBytes']


def _encoded_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def test_a_head_larger_than_one_frame_is_alone_on_page_zero():
    turn = {'turnId': 't', 'projectionRevision': 3, 'projection': {
        'pdfTexts': [{'text': 'p' * _FRAME_PAYLOAD_BYTES}],
        'toolRounds': [{'toolCallId': 'one'}, {'toolCallId': 'two'}],
    }}
    head = turn_projection_page(turn, 0)
    rest = turn_projection_page(turn, 1)

    assert head['pageCount'] == 2 and head['members'] == {}
    assert head['pagedMembers'] == {'toolRounds': 2}
    assert head['turn']['projection'] == {'pdfTexts': turn['projection']['pdfTexts']}
    assert rest['members'] == {'toolRounds': {'start': 0, 'items': turn['projection']['toolRounds']}}
    assert _encoded_bytes(rest) <= _FRAME_PAYLOAD_BYTES
    assert turn_projection_page(turn, 2) is None


def test_members_of_another_type_stay_in_the_head_unpaged():
    turn = {'turnId': 't', 'projectionRevision': 1, 'projection': {
        'toolRounds': None, 'segments': {'not': 'a list'}, 'content': '', 'thinking': 7,
    }}
    page = turn_projection_page(turn, 0)

    assert page['pageCount'] == 1 and page['members'] == {}
    assert page['pagedMembers'] == {'content': 0}
    assert page['turn']['projection'] == {
        'toolRounds': None, 'segments': {'not': 'a list'}, 'thinking': 7}
